"""Sessions: Jarvis's other chats, addressable by name.

Every conversation is a session — its own history, its own persona, its own idea of what is
going on. Until now they were sealed from each other: what was worked out in one chat had to be
carried to another by Arsen, by hand. So a chat can now be *addressed*: ``sessions.list`` says
who is there, ``sessions.read`` catches up on one, and ``sessions.say`` writes into it — which
wakes it if it is idle and joins it if a run is already working (a steer).

A handle is the title, slugged: "Домо — етажна собственост" is ``@домо-етажна-собственост`` and
answers to ``@домо`` as long as no other chat starts that way. Nothing is stored: the handle IS
the title, so renaming a chat renames its session and there is no second name to drift. Ambiguity
is refused with the candidates rather than guessed.

Three rules the design turns on:

- **Only when Arsen says so.** He decided (2026-09-19) that Jarvis does not strike up
  conversations with itself; these tools are for "ask the domo chat when the meeting is", not for
  a run that wanders off to consult its siblings. The description says so, and every message
  lands visibly in both transcripts.
- **An incognito chat is not a session.** It is never listed, never read and never written to:
  what is said there stays there, and a sibling asking politely is still a leak.
- **A chain that comes back on itself stops.** A message carries the conversations it has passed
  through; a session already in that chain cannot be written to again, and the chain may not grow
  past ``settings.sessions.max_hops``. Two Jarvises talking to each other for ever is the one
  failure mode a feature like this owes an answer for.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from jarvis_core.engine.bus import Subscriber
from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import Conversation, ConversationActivity, ConversationKind, RunKind, RunStatus, ToolResult

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

MAX_TEXT = 4_000
READ_MAX = 50
#: The kinds a session list offers by name. The rest — a schedule's own chat, a triage batch, a
#: meeting — are the machine talking to itself; they can still be addressed by id or handle.
ADDRESSABLE_KINDS = frozenset({ConversationKind.CHAT, ConversationKind.COLLAB})
#: How many chains to remember. A chain is four short strings; this is a rounding error in RAM
#: and covers every run a day could hold.
_CHAINS_KEPT = 500


def handle_of(title: str) -> str:
    """The title as a handle: lowercase, one hyphen between words, letters and digits only.

    Cyrillic is kept as it is — the chats are named in Bulgarian and ``@домо`` is what Arsen
    would type. An empty result (a title of only punctuation) falls back to ``chat``.
    """
    out: list[str] = []
    for ch in title.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:40] or "chat"


def _born(conv: Conversation) -> tuple[datetime, str]:
    """When it was made, comparably. Rows imported from V1 carry a naive timestamp and sorting
    those beside today's aware ones raises — which is how the live fleet found this and the
    tests, all of whose chats were minutes old, did not (2026-09-19)."""
    born = conv.created_at
    return (born.replace(tzinfo=UTC) if born.tzinfo is None else born, conv.id)


def handles_for(conversations: list[Conversation]) -> dict[str, str]:
    """Conversation id → handle, made unique oldest-first: the second "Домо" is ``домо-2``."""
    taken: dict[str, int] = {}
    out: dict[str, str] = {}
    for conv in sorted(conversations, key=_born):
        base = handle_of(conv.title)
        n = taken.get(base, 0) + 1
        taken[base] = n
        out[conv.id] = base if n == 1 else f"{base}-{n}"
    return out


class SessionNotFound(LookupError):
    pass


class _ListArgs(BaseModel):
    only_active: bool = Field(default=False, description="Only the chats with a run working or waiting right now")
    include_archived: bool = Field(default=False, description="Include archived chats")
    limit: int = Field(default=25, ge=1, le=100)


class _SayArgs(BaseModel):
    to: str = Field(description="The session's handle (with or without @), or its conversation id")
    text: str = Field(description="What to say, in full. The other session sees it as a message from this one.")
    wait: bool = Field(default=True, description="Wait for that session's reply and return it. False just delivers it.")
    timeout_s: int = Field(default=0, ge=0, le=900, description="0 = the configured default")


class _ReadArgs(BaseModel):
    of: str = Field(description="The session's handle (with or without @), or its conversation id")
    limit: int = Field(default=10, ge=1, le=READ_MAX, description="How many of the last messages")


class SessionsTools(BuiltinProvider):
    """The three tools, over the store and the engine the core already has."""

    name = "sessions"

    def __init__(self, core: Core) -> None:
        self._core = core
        #: run id → the conversations a message has passed through to get here.
        self._chains: dict[str, tuple[str, ...]] = {}
        super().__init__()

    # --- the sessions themselves -------------------------------------------------------------

    async def _sessions(self, *, include_archived: bool = False, all_kinds: bool = False) -> list[Conversation]:
        """The chats that may be addressed, newest first. Incognito is never one of them, and by
        default neither are the machine's own: the live core had 259 conversations, most of them a
        scheduled run's chat named after its date. `@2026-09-19` is nobody's idea of a session."""
        convs = await self._core.store.list_conversations(include_archived=include_archived)
        return [c for c in convs if not c.incognito and (all_kinds or c.kind in ADDRESSABLE_KINDS)]

    async def resolve(self, who: str, *, include_archived: bool = True) -> Conversation:
        """A handle, a prefix of one, or a conversation id → the conversation. Never a guess.

        Every kind is searched, not only the ones the list offers: a scheduled run's chat is a
        session too once Arsen has named it."""
        wanted = who.strip().lstrip("@").lower()
        if not wanted:
            raise SessionNotFound("no session named")
        convs = await self._sessions(include_archived=include_archived, all_kinds=True)
        by_id = {c.id: c for c in convs}
        if wanted in by_id:
            return by_id[wanted]
        handles = handles_for(convs)
        exact = [c for c in convs if handles[c.id] == wanted]
        if exact:
            return exact[0]
        near = [c for c in convs if handles[c.id].startswith(wanted) or wanted in c.title.lower()]
        if len(near) == 1:
            return near[0]
        if not near:
            raise SessionNotFound(f"no session @{wanted}")
        names = ", ".join(f"@{handles[c.id]}" for c in near[:8])
        raise SessionNotFound(f"@{wanted} could be any of: {names} — say which")

    def _chain(self) -> tuple[str, ...]:
        run_id = current_run_id.get()
        here = current_conversation_id.get()
        chain = self._chains.get(run_id or "", ())
        return chain if here is None or here in chain else (*chain, here)

    def _remember(self, run_id: str, chain: tuple[str, ...]) -> None:
        if len(self._chains) >= _CHAINS_KEPT:
            for old in list(self._chains)[: _CHAINS_KEPT // 4]:
                del self._chains[old]
        self._chains[run_id] = chain

    async def handles_block(self, *, exclude: str | None = None, limit: int = 20) -> str:
        """The handles as the per-turn context shows them (engine/context.SessionsBlock)."""
        convs = await self._sessions()
        handles = handles_for(convs)
        rows = [c for c in convs if c.id != exclude][:limit]
        return "\n".join(f"- @{handles[c.id]} — {c.title}" for c in rows)

    # --- the tools ---------------------------------------------------------------------------

    @tool(
        "sessions.list",
        description=(
            "The other chats (sessions) you can talk to, with the @handle each answers to, what it is about "
            "and whether a run is working in it right now. A session that is idle can still be written to — "
            "sessions.say wakes it. Private (incognito) chats are never listed."
        ),
        args=_ListArgs,
        read_only=True,
        idempotent=True,
    )
    async def _list(self, only_active: bool = False, include_archived: bool = False, limit: int = 25) -> ToolResult:
        convs = await self._sessions(include_archived=include_archived, all_kinds=only_active)
        handles = handles_for(convs)
        here = current_conversation_id.get()
        rows = [c for c in convs if not only_active or c.activity is not ConversationActivity.IDLE]
        if not rows:
            return ToolResult.data("No other sessions." if not only_active else "No session is working right now.")
        lines = []
        for conv in rows[:limit]:
            mark = " (this one)" if conv.id == here else ""
            state = {"running": "working", "waiting": "waiting on Arsen"}.get(conv.activity.value, "idle")
            when = conv.updated_at.strftime("%Y-%m-%d %H:%M")
            archived = ", archived" if conv.archived else ""
            lines.append(
                f"@{handles[conv.id]} — {conv.title} [{state}{archived}, {conv.message_count} msgs, {when}]{mark}"
            )
        more = f"\n…and {len(rows) - limit} more" if len(rows) > limit else ""
        return ToolResult.data("\n".join(lines) + more)

    @tool(
        "sessions.read",
        description=(
            "The last messages of another session, to catch up on what it knows before writing to it "
            "or before answering Arsen about it. Read-only: it does not wake that session or tell it anything."
        ),
        args=_ReadArgs,
        read_only=True,
        idempotent=True,
    )
    async def _read(self, of: str, limit: int = 10) -> ToolResult:
        try:
            conv = await self.resolve(of)
        except SessionNotFound as exc:
            return ToolResult.failure(str(exc))
        if conv.id == current_conversation_id.get():
            return ToolResult.failure("that is this session; its messages are already in front of you")
        messages = await self._core.store.list_messages(conv.id, limit=limit)
        useful = [m for m in messages if m.role.value in {"user", "assistant"} and m.content.strip()]
        if not useful:
            return ToolResult.data(f"@{handle_of(conv.title)} ({conv.title}) has nothing said in it yet.")
        lines = [f"@{handle_of(conv.title)} — {conv.title}, last {len(useful)} messages:"]
        for m in useful:
            who = "Arsen" if m.role.value == "user" and not m.name else (m.name or "Jarvis")
            lines.append(f"[{m.created_at.strftime('%m-%d %H:%M')}] {who}: {m.content.strip()[:600]}")
        return ToolResult.data("\n".join(lines))

    @tool(
        "sessions.say",
        description=(
            "Say something to another session of yours — it arrives as a message from this session, wakes it "
            "if it is idle and joins the run if one is already working there. Use it ONLY when Arsen asks you "
            "to talk to another chat ('ask the X chat…', 'tell X that…', 'sync them'); never to consult your "
            "other selves on your own initiative. With wait=true (the default) you get that session's reply back."
        ),
        args=_SayArgs,
    )
    async def _say(self, to: str, text: str, wait: bool = True, timeout_s: int = 0) -> ToolResult:
        cfg = self._core.settings.sessions
        if not cfg.enabled:
            return ToolResult.failure("talking between sessions is switched off (settings.sessions.enabled)")
        body = " ".join(text.split())
        if not body:
            return ToolResult.failure("nothing to say")
        if len(body) > MAX_TEXT:
            return ToolResult.failure(f"a message of {len(body)} characters; the ceiling is {MAX_TEXT}")
        try:
            target = await self.resolve(to)
        except SessionNotFound as exc:
            return ToolResult.failure(str(exc))
        here = current_conversation_id.get()
        if target.id == here:
            return ToolResult.failure("that is this session — say it to Arsen instead")
        chain = self._chain()
        if target.id in chain:
            return ToolResult.failure(
                f"@{handle_of(target.title)} is already in this chain of messages; answer rather than write back"
            )
        if len(chain) > cfg.max_hops:
            return ToolResult.failure(
                f"this message has already passed through {len(chain)} sessions (max_hops={cfg.max_hops})"
            )
        sender = await self._core.store.get_conversation(here) if here else None
        from_name = f"@{handle_of(sender.title)}" if sender else "@jarvis"
        return await self._deliver(target, body, from_name, (*chain, target.id), wait, timeout_s or cfg.reply_timeout_s)

    # --- delivery ----------------------------------------------------------------------------

    async def _deliver(
        self, target: Conversation, body: str, from_name: str, chain: tuple[str, ...], wait: bool, timeout_s: int
    ) -> ToolResult:
        """Into a working run as a steer, or as a new run that wakes the session."""
        text = f"[{from_name}] {body}"
        working = self._core.engine.working_run_in(target.id)
        if working and self._core.engine.steer(working, text):
            self._remember(working, chain)
            if not wait:
                return ToolResult.data(f"Said to @{handle_of(target.title)}; it was already working and took it in.")
            return await self._wait_for(target, working, from_name, timeout_s)
        run, _ = await self._core.engine.create_run(text=text, conversation_id=target.id, kind=RunKind.COLLAB)
        self._remember(run.id, chain)
        if not wait:
            return ToolResult.data(f"Woke @{handle_of(target.title)} with it; it is working on a reply.")
        return await self._wait_for(target, run.id, from_name, timeout_s)

    async def _wait_for(self, target: Conversation, run_id: str, from_name: str, timeout_s: int) -> ToolResult:
        sub = Subscriber(name=f"sessions:{from_name}")
        sub.conversations.add(target.id)
        self._core.bus.attach(sub)
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    ev = await sub.queue.get()
                    if getattr(ev, "run_id", None) != run_id:
                        continue
                    if ev.type in {"run.done", "run.failed", "run.cancelled"}:
                        break
        except TimeoutError:
            return ToolResult.data(
                f"@{handle_of(target.title)} has the message but is still working after {timeout_s}s — "
                "read it later with sessions.read."
            )
        finally:
            self._core.bus.detach(sub)
        return ToolResult.data(await self._reply_of(target, run_id))

    async def _reply_of(self, target: Conversation, run_id: str) -> str:
        run = await self._core.store.get_run(run_id)
        head = f"@{handle_of(target.title)} says"
        if run is not None and run.status is RunStatus.FAILED:
            return f"{head}: [its run failed] {run.error}"
        for m in reversed(await self._core.store.list_run_messages(run_id)):
            if m.role.value == "assistant" and m.content.strip():
                return f"{head}: {m.content.strip()}"
        return f"{head} nothing (its run ended without a reply)."


def describe(conv: Conversation, handle: str) -> dict[str, Any]:
    """The session as the API hands it to the web (Settings → sessions, the composer's @ list)."""
    return {
        "conversation_id": conv.id,
        "handle": handle,
        "title": conv.title,
        "activity": conv.activity.value,
        "archived": conv.archived,
        "message_count": conv.message_count,
        "updated_at": conv.updated_at.isoformat(),
    }
