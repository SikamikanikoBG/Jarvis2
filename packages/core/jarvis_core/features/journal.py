"""The day journal: where every conversation stands today (docs/stories/12_assistant.md).

Each chat is its own world, which is right for work and wrong for an assistant: on a call in the
OLX chat, "anything else interesting today?" was answered about OLX twice, because OLX was all
the call could see (2026-10-13). The day had six other things going on, all in scheduled chats.

So when a run ends, its conversation's **digest** for the day is rewritten in the background:
one or two sentences on where it stands, and what - if anything - it waits on Arsen for. It is
written on the housekeeping lane, never in front of a reply, and a few seconds after the run so
that runs ending together are folded into one write per conversation. The **overview** is the
day's digests in one block: waiting on Arsen first, then the newest, the routine ones that found
nothing new counted at the end. A call and the Jarvis conversation carry it with every turn
(``TodayBlock``; an unchanged block costs one line, engine/context.py); any chat can ask for it
with ``sessions.today``.

Nothing here can fail a run or slow one down: a model that does not answer leaves the last
digest, or a plain one made from the run's own answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis_core.features import episodes
from jarvis_core.features.knowledge import parse_json
from jarvis_core.models.base import BACKGROUND, ModelAdapter, ModelTextChunk, call_urgency
from jarvis_core.models.ledger import call_purpose
from jarvis_proto import Channel, Conversation, ConversationKind, Message, Role, Run, RunKind, RunStatus

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

#: Runs that end within this of each other are written up once.
DEBOUNCE_S = 6.0
#: At most this many missing digests are caught up per look at the day.
CATCH_UP_MAX = 40
#: How long one digest may take before the plain fallback is written instead.
MODEL_TIMEOUT_S = 90.0


class ModelUnavailable(RuntimeError):
    """The model did not answer at all (unreachable, failed, timed out) - as opposed to an answer
    that could not be read. The back-fill waits for the first; only the second costs it a try."""


#: The kinds that are the machine's own plumbing, not something Arsen would call "today".
# A task's own conversation is not a line of the day: its report in the chat that asked is.
SKIPPED_KINDS = frozenset({ConversationKind.TRIAGE, ConversationKind.ARCHIVE, ConversationKind.TASK})
_KIND_LABEL = {
    ConversationKind.CHAT: "chat",
    ConversationKind.COLLAB: "chat",
    ConversationKind.SCHEDULED: "scheduled",
    ConversationKind.MEETING: "meeting",
    ConversationKind.MAIL: "mail",
}

_DIGEST_PROMPT = (
    """You keep {user}'s assistant's journal of the day: one entry per conversation,
saying where that conversation stands today. Update the entry for this conversation.

Conversation: "{title}" ({kind})
Its entry so far today: {previous}
Its last episode today: {last_episode}
The runs since then, oldest first:
{runs}

Return JSON only: {{"summary": "...", "waiting": "...", "quiet": false, "episode": {{"continues": false,
"title": "...", "what": "...", "people": [], "places": [], "importance": 2, "valence": "neutral"}}}}
- summary: at most two short English sentences, 40 words in all, on where this conversation
  stands today - what was done or found and what came of it, with the names and numbers that
  matter. Merge it with the entry so far and drop what the latest run superseded. Write it as a
  note to yourself: no preamble, never "the assistant" or "{user} asked", and do not repeat the
  waiting part.
- waiting: what it needs from {user} before it can go on, as a short phrase; "" if nothing.
- quiet: true only when the run was a routine check that found nothing new worth mentioning.
"""
    + episodes.ASK
)

#: The runs an episode has not seen yet, written up at once (a call's turns end seconds apart).
RUNS_MAX = 5


@dataclass(slots=True)
class Digest:
    conversation_id: str
    summary: str
    waiting: str
    quiet: bool
    updated_at: datetime


class DayJournal:
    def __init__(self, core: Core, model: Callable[[], ModelAdapter]) -> None:
        self._core = core
        self._model = model
        self.episodes = episodes.EpisodeStore(core.db)
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # One digest at a time: they are background work on a shared GPU.
        self._sem = asyncio.Semaphore(1)
        #: (day, conversation id) already asked to catch up, so a missing digest is asked for once.
        self._caught_up: set[tuple[str, str]] = set()

    def catch_up(self, conversation_ids: list[str]) -> None:
        """Write the digests today's conversations do not have yet - runs that ended before the
        journal existed, or before a restart. Asked for once per conversation per day; written
        one at a time in the background like any other (2026-10-13: the first call after the
        deploy found the day as titles and half-sentences, and made nothing of it)."""
        if not self._core.settings.assistant.journal:
            return
        day = self.today().isoformat()
        for cid in conversation_ids[:CATCH_UP_MAX]:
            if (day, cid) in self._caught_up or cid in self._pending:
                continue
            self._caught_up.add((day, cid))
            self._start(cid)

    # --- when a run ends ---------------------------------------------------------------------

    def note(self, run: Run) -> None:
        """A run ended: its conversation's digest is rewritten a few seconds from now."""
        s = self._core.settings.assistant
        if not s.journal or run.kind is RunKind.TRIAGE:
            return
        if run.status not in (RunStatus.DONE, RunStatus.FAILED, RunStatus.WAITING_USER):
            return  # a cancelled run changed nothing worth writing down
        handle = self._pending.pop(run.conversation_id, None)
        if handle is not None:
            handle.cancel()
        loop = asyncio.get_running_loop()
        self._pending[run.conversation_id] = loop.call_later(DEBOUNCE_S, self._start, run.conversation_id)

    def _start(self, conversation_id: str) -> None:
        self._pending.pop(conversation_id, None)
        task = asyncio.create_task(self._write(conversation_id), name=f"journal:{conversation_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def flush(self) -> None:
        """Write everything that is waiting now (tests, shutdown)."""
        for cid, handle in list(self._pending.items()):
            handle.cancel()
            self._start(cid)
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _write(self, conversation_id: str) -> None:
        call_urgency.set(BACKGROUND)  # not the urgency of the run whose ending scheduled it
        call_purpose.set("journal")  # booked as housekeeping on the Context screen, not to the run
        try:
            async with self._sem:
                await self._digest(conversation_id)
        except Exception as exc:  # housekeeping: never fails anything
            log.warning("journal: %s skipped: %s", conversation_id, exc)

    async def _digest(self, conversation_id: str) -> None:
        store = self._core.store
        conv = await store.get_conversation(conversation_id)
        if conv is None or conv.incognito or conv.kind in SKIPPED_KINDS:
            return
        runs = [
            r
            for r in await store.list_runs(conversation_id, limit=RUNS_MAX)
            if r.status.terminal or r.status is RunStatus.WAITING_USER
        ]
        if not runs:
            return
        run = runs[0]
        day = self.today()
        previous = await self._row(conversation_id, day)
        told = await self.episodes.of_day(conversation_id, day.isoformat())
        last = told[-1] if told else None
        fresh = self._unseen(runs, last, day)
        exchanges: list[tuple[Run, str, str]] = []
        for r in fresh:
            asked, said = await self._exchange(r)
            # A secret Jarvis read out in this chat is never written into the day's journal
            # (docs/stories/14_agent_admin.md): the stored values themselves are masked, no word list.
            exchanges.append((r, self._core.admin.redact(asked), self._core.admin.redact(said)))
        answer = exchanges[-1][2]
        data = await self._ask(conv, exchanges, previous, last)
        if data is None:
            # What the day already says about it comes before the answer's first words.
            summary = (last.title if last else "") or _first_sentences(answer or exchanges[-1][1], 240) or conv.title
            waiting = _first_sentences(run.waiting_reason or "", 120) if run.status is RunStatus.WAITING_USER else ""
            quiet = False
        else:
            summary = " ".join(str(data.get("summary") or "").split())[:320] or _first_sentences(answer, 240)
            waiting = " ".join(str(data.get("waiting") or "").split())[:160]
            quiet = bool(data.get("quiet")) and not waiting
            episode = episodes.from_model(
                None if quiet else data.get("episode"),
                conversation_id=conversation_id,
                day=day.isoformat(),
                started_at=fresh[0].created_at,
                ended_at=run.finished_at or run.created_at,
                first_run_id=fresh[0].id,
                last_run_id=run.id,
                last=last,
            )
            if episode is not None:
                await self.episodes.save(episode)
        if not summary:
            return
        await self._core.db.execute(
            "INSERT INTO conversation_digests(conversation_id, day, summary, waiting, quiet, run_id, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(conversation_id, day) DO UPDATE SET summary = excluded.summary,"
            " waiting = excluded.waiting, quiet = excluded.quiet, run_id = excluded.run_id,"
            " updated_at = excluded.updated_at",
            (conversation_id, day.isoformat(), summary, waiting, int(quiet), run.id, datetime.now(UTC).isoformat()),
        )

    async def past_episode(self, conv: Conversation, runs: list[Run], day: date) -> bool:
        """Write up ``runs`` (one past day of ``conv``, oldest first) as the live journal would have
        that day: the same question, the episode it was in before them. Only the episode is kept:
        the day's digest stays as it was written then (features/backfill.py). False when the model's
        answer could not be read; ``ModelUnavailable`` when it gave none, so the caller can wait."""
        told = [e for e in await self.episodes.of_day(conv.id, day.isoformat()) if e.started_at <= runs[0].created_at]
        last = told[-1] if told else None
        exchanges: list[tuple[Run, str, str]] = []
        for r in runs:
            asked, said = await self._exchange(r)
            exchanges.append((r, self._core.admin.redact(asked), self._core.admin.redact(said)))
        previous = await self._row(conv.id, day)
        call_urgency.set(BACKGROUND)
        call_purpose.set("journal")
        async with self._sem:  # one write-up at a time, with the live journal's own
            data = await self._ask(conv, exchanges, previous, last, strict=True)
        if data is None:
            return False
        quiet = bool(data.get("quiet")) and not " ".join(str(data.get("waiting") or "").split())
        episode = episodes.from_model(
            None if quiet else data.get("episode"),
            conversation_id=conv.id,
            day=day.isoformat(),
            started_at=runs[0].created_at,
            ended_at=runs[-1].finished_at or runs[-1].created_at,
            first_run_id=runs[0].id,
            last_run_id=runs[-1].id,
            last=last,
        )
        if episode is not None:
            await self.episodes.save(episode)
        return True

    def _unseen(self, runs: list[Run], last: episodes.Episode | None, day: date) -> list[Run]:
        """The runs the last episode has not got yet, oldest first (``runs`` is newest first)."""
        tz = ZoneInfo(self._core.settings.timezone)
        fresh: list[Run] = []
        for r in runs:
            if last is not None and r.id == last.last_run_id:
                break
            if r.created_at.astimezone(tz).date() == day:
                fresh.append(r)
        return list(reversed(fresh)) or [runs[0]]

    async def _exchange(self, run: Run) -> tuple[str, str]:
        """What was asked and what was answered last in this run."""
        msgs = await self._core.store.list_run_messages(run.id)
        request = next((m.content for m in msgs if m.role is Role.USER and not m.name), run.input_text)
        answer = next(
            (m.content for m in reversed(msgs) if m.role is Role.ASSISTANT and not m.tool_calls and m.content.strip()),
            "",
        )
        if run.status is RunStatus.FAILED and not answer:
            answer = f"(the run failed: {run.error or 'no reason given'})"
        if run.status is RunStatus.WAITING_USER and run.waiting_reason:
            answer = f"{answer}\n(waiting for {self._core.settings.user_name}: {run.waiting_reason})".strip()
        return request, answer

    async def _ask(
        self,
        conv: Conversation,
        exchanges: list[tuple[Run, str, str]],
        previous: Digest | None,
        last: episodes.Episode | None,
        *,
        strict: bool = False,
    ) -> dict[str, Any] | None:
        """The model's answer as JSON, None if it could not be read. A model that did not answer
        is None as well (the live journal then writes the digest plainly), or, ``strict``,
        ``ModelUnavailable``."""
        s = self._core.settings
        tz = ZoneInfo(s.timezone)
        status = {"done": "finished", "failed": "failed", "waiting_user": "waiting for an answer"}
        # The newest run in full; what came before it just now, shorter.
        runs = "\n".join(
            f"[{(r.finished_at or r.created_at).astimezone(tz):%Y-%m-%d %H:%M}, {status.get(r.status.value, r.status.value)}]\n"
            f"REQUEST: {_clip(asked, 1500)}\nANSWER: {_clip(said, 3000 if i == len(exchanges) - 1 else 900)}"
            for i, (r, asked, said) in enumerate(exchanges)
        )
        told = f'"{last.title}" - {last.what} (people: {", ".join(last.people) or "-"})' if last else "(none yet)"
        prompt = _DIGEST_PROMPT.format(
            user=s.user_name,
            title=conv.title,
            kind=_KIND_LABEL.get(conv.kind, conv.kind.value),
            previous=previous.summary if previous else "(none yet)",
            last_episode=told,
            runs=runs,
        )
        try:
            async with asyncio.timeout(MODEL_TIMEOUT_S):
                text = ""
                async for chunk in self._model().stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                    if isinstance(chunk, ModelTextChunk):
                        text += chunk.text
        except Exception as exc:
            if strict:
                raise ModelUnavailable(str(exc) or type(exc).__name__) from exc
            log.info("journal: model unavailable for %s (%s); writing it plainly", conv.id, exc)
            return None
        return parse_json(text)

    # --- reading -----------------------------------------------------------------------------

    def today(self) -> date:
        return datetime.now(ZoneInfo(self._core.settings.timezone)).date()

    async def _row(self, conversation_id: str, day: date) -> Digest | None:
        row = await self._core.db.fetchone(
            "SELECT * FROM conversation_digests WHERE conversation_id = ? AND day = ?",
            (conversation_id, day.isoformat()),
        )
        return _digest(row) if row else None

    async def digests(self, day: date | None = None) -> list[Digest]:
        rows = await self._core.db.fetchall(
            "SELECT * FROM conversation_digests WHERE day = ? ORDER BY updated_at DESC",
            ((day or self.today()).isoformat(),),
        )
        return [_digest(r) for r in rows]

    async def overview(self, *, exclude: str | None = None, max_chars: int | None = None) -> str | None:
        """The day across every conversation but ``exclude``: one line each, waiting on Arsen
        first, then the newest; routine ones that found nothing are named at the end. None when
        nothing else happened today."""
        from jarvis_core.features.sessions import handles_for

        s = self._core.settings
        cap = max_chars or s.assistant.overview_chars
        tz = ZoneInfo(s.timezone)
        day = self.today()
        convs = [c for c in await self._core.store.list_conversations(include_archived=True) if not c.incognito]
        handles = handles_for(convs)  # the same handles sessions.read resolves
        digests = {d.conversation_id: d for d in await self.digests(day)}
        # Today's conversations: those with a digest, and those that came alive today without
        # one yet (written a few seconds after their run, or still working).
        today = [
            c
            for c in convs
            if c.id != exclude
            and c.kind not in SKIPPED_KINDS
            and (c.id in digests or c.updated_at.astimezone(tz).date() == day)
            and c.message_count > 0
        ]
        if not today:
            return None
        # Conversations active today with no line yet: written now, in the background, for the
        # next look. Until then they are shown by title.
        self.catch_up([c.id for c in today if c.id not in digests and c.activity.value != "running"])

        def line(c: Conversation) -> str:
            d = digests.get(c.id)
            when = (d.updated_at if d else c.updated_at).astimezone(tz).strftime("%H:%M")
            head = f"@{handles[c.id]} ({_KIND_LABEL.get(c.kind, c.kind.value)}, {when})"
            if c.activity.value == "running":
                head += " [working now]"
            if d is None:
                return f"- {head}: {c.title}" + (f" - {_first_sentences(c.preview or '', 160)}" if c.preview else "")
            wait = f" WAITING ON {s.user_name.upper()}: {d.waiting}." if d.waiting else ""
            return f"- {head}:{wait} {d.summary}"

        def order(c: Conversation) -> tuple[int, float]:
            d = digests.get(c.id)
            stamp = (d.updated_at if d else c.updated_at).timestamp()
            return (0 if d and d.waiting else 1, -stamp)

        loud = sorted((c for c in today if not (digests.get(c.id) and digests[c.id].quiet)), key=order)
        quiet = sorted((c for c in today if digests.get(c.id) and digests[c.id].quiet), key=order)
        head = (
            f"## Today in {s.user_name}'s other conversations\n"
            "Where each conversation with activity today stands, written by you after its runs - "
            f"those waiting on {s.user_name} first. When he asks how his day is going, what is new or what "
            "waits on him, this is what he means: answer from these lines, now. These are other chats, not "
            "this one: read one with "
            'sessions.read(of="@handle") before going into detail, and refresh the list with sessions.today.'
        )
        lines: list[str] = []
        used = len(head)
        left: list[Conversation] = []
        for c in loud:
            text = line(c)
            if used + len(text) + 1 > cap and lines:
                left.append(c)
                continue
            lines.append(text)
            used += len(text) + 1
        if quiet:
            names = ", ".join(f"@{handles[c.id]}" for c in quiet)
            lines.append(_clip(f"- Routine, nothing new: {names}", max(80, cap - used)))
        if left:
            names = ", ".join(f"@{handles[c.id]}" for c in left)
            lines.append(_clip(f"- And {len(left)} more today: {names}", max(80, cap - used)))
        return head + "\n" + "\n".join(lines)


class TodayBlock:
    """The day's overview in a turn's context: on a call (where a question about the day must
    not cost a round trip of silence) and in the Jarvis conversation, which is for exactly that.
    Everywhere else it is one tool call away (``sessions.today``)."""

    def __init__(self, journal: DayJournal, settings: Callable[[], Any]) -> None:
        self._journal = journal
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        s = self._settings().assistant
        home = s.home_conversation_id is not None and run.conversation_id == s.home_conversation_id
        call = run.channel is Channel.VOICE and s.overview_on_calls
        if not (home or call) or run.kind is RunKind.TRIAGE:
            return None
        return await self._journal.overview(exclude=run.conversation_id)


def _digest(row: Any) -> Digest:
    stamp = datetime.fromisoformat(row["updated_at"])
    return Digest(
        conversation_id=row["conversation_id"],
        summary=row["summary"],
        waiting=row["waiting"],
        quiet=bool(row["quiet"]),
        updated_at=stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC),
    )


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _first_sentences(text: str, limit: int) -> str:
    """The start of a text, cut at a sentence end when there is one within ``limit``."""
    flat = " ".join(text.replace("#", " ").replace("*", " ").split())
    if len(flat) <= limit:
        return flat
    cut = max(flat.rfind(". ", 0, limit), flat.rfind("! ", 0, limit), flat.rfind("? ", 0, limit))
    return flat[: cut + 1] if cut > 40 else flat[: limit - 1] + "…"
