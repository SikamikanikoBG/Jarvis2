"""Context assembly: one system message built from providers in a cache-friendly order, then
the (compacted, trimmed) history.

Order: static rules → boards (change rarely) → skills (per message) → knowledge (per message)
→ browser page (per message) → plan (per step) → date. Nothing here calls a model: the
compactor summarises in the background, after a run (``compact_if_due``), never in a reply's way.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from jarvis_core.db import Store
from jarvis_core.engine.views import admit, aged, is_view, short_ref
from jarvis_core.features.personality import personality_block
from jarvis_proto import (
    Attachment,
    AttachmentKind,
    Channel,
    ContextBreakdown,
    Message,
    Plan,
    Role,
    Run,
    RunKind,
    Settings,
    ToolSpec,
)

# One numbered list behind a precedence block. Count, not length, is what degrades the
# local model (V1 lesson), so keep this short and add rules only when a test demands one.
SYSTEM_RULES = """You are {assistant_name}, the personal assistant of {user_name}.

PRECEDENCE: when rules conflict, the lower-numbered rule wins.
1. Never claim to have read, checked, sent or done something unless a tool result in this
   conversation shows it. If you did not use a tool, say what you would need to check.
2. Use tools for anything that depends on live data (time, files, mail, web). Do not guess.
3. Do the task; do not narrate your plan unless asked. Ask one precise question only when
   a required detail is genuinely missing.
4. Keep answers short and direct. No filler, no emojis, no restating the question.
5. {language_hint}
6. A long tool result arrives as an outline of sections with @refs; the whole is stored.
   jarvis.result_read(ref="@x.3") shows a section ("@x.3-5" a range), jarvis.result_search(ref="@x",
   pattern=...) finds inside — never re-run the tool for it. [partial] with a cursor means the
   server holds more: page it before concluding. Bulk work (dozens of mails, tasks, files or
   rows to judge) is not read through: write a script on a host that fetches, groups and
   cuts each item to a short record, run it, and decide on its compact output.
7. When a tool fails, say so plainly and try one sensible alternative, not the same call.
8. Format for a phone screen: short paragraphs, lists only when they add clarity.
9. Notes boards and known context below are facts Arsen curated; prefer them over guesses,
   and use notes.add when Arsen asks you to remember something."""

# The starting guess. Real prompts calibrate it: a step's chars against the server's own
# prompt_tokens (``ContextAssembler.observe``) — minified JSON and Cyrillic run nearer 2 chars a
# token than 3.2, and a 60% under-count is a ceiling that is not one (measured 2026-09-16:
# estimated 50k, served 79k).
_CHARS_PER_TOKEN = 3.2
_CPT_MIN, _CPT_MAX = 1.5, 4.5

# A tool result that does not fit is shown as a VIEW - sections with refs, see engine/views.py -
# at one of two sizes: the admission limit from the step it arrives in, and AGED_VIEW_CHARS once
# the step is over, the run's results outgrow their budget, or it belongs to an earlier turn.
# The DB keeps the full text and jarvis.result_read hands any section back. (Before 2026-09-20
# this was the first 700 chars of the text - for JSON, one hex store_id - and the rest was
# reachable only by character offset; the 43k folder tree made the model fish for six minutes.)
is_admitted_head = is_view  # the breakdown counts results that ride as views


def _skill_names(context_text: str) -> list[str]:
    return [ln[len("## Skill: ") :].strip() for ln in context_text.splitlines() if ln.startswith("## Skill: ")]


def earlier_turn_view(
    history: list[Message],
    current_run_id: str,
    *,
    admit_chars: int | None = None,
    structured: Collection[str] = (),
) -> list[Message]:
    """The history as the model should see it: earlier turns kept, but at the size they are worth.

    A finished turn's tool results were acted on when they arrived; what matters now is the
    answer the model wrote from them, so they ride as heads. The context block injected for an
    earlier request (skills, knowledge, a browser page) served that request and is a stub here,
    naming the skills it carried. The current run is untouched: its results are what the model
    is answering, and its context is what this request needs.

    This runs BEFORE compaction and trimming so both count what will actually be sent. Counting
    the full 41k-character folder dump of an earlier turn - when the model would only ever have
    been shown 700 characters of it - made the trim throw the whole previous turn away, answer
    included, and the next question was answered from scratch (2026-09-11, the AI Masterclass
    chat: three turns, the mail hunted for three times).

    A context message stored as blocks (its id in ``structured``) is left as it is here:
    :func:`render_context` decides block by block what of it is still worth its tokens.
    """
    out: list[Message] = []
    for m in history:
        if m.run_id == current_run_id:
            # A resumed run rebuilds its own results from the DB: they enter at admission size,
            # exactly as they did when they first arrived.
            out.append(admit(m, admit_chars) if admit_chars and m.role is Role.TOOL else m)
        elif m.role is Role.TOOL:
            out.append(aged(m))
        elif m.role is Role.USER and m.name == "context" and m.id in structured:
            out.append(m)
        elif m.role is Role.USER and m.name == "context":
            names = _skill_names(m.content)
            out.append(m.model_copy(update={"content": _not_repeated(names)}))
        else:
            out.append(m)
    return out


def _not_repeated(skills: list[str]) -> str:
    # Worded as "not repeated", never "omitted": "omitted here" read as "lost", and the model
    # announced it no longer had the previous turn and started over (2026-10-02, "само прати
    # мейла, вече имаш всичко" after the SmartLab digest).
    note = "[The context block for the request above is not repeated here"
    return note + (
        f"; it carried the skills {', '.join(skills)} - skills.use(name) shows one again]" if skills else "]"
    )


# --- context blocks: a block given once is not given again -------------------------------
#
# A turn's context (skills, known context, the voice style, the day's overview) is stored as
# blocks. A block identical to one already given in the conversation is written as a reference,
# and the earlier copy stays whole for as long as any later turn refers to it. Before this the
# previous turn's copy was cut to a stub and the new turn sent the whole thing again: in a skill
# chat ~3k tokens re-read on every turn, which on a call was most of the 5 s before the first
# word (2026-10-13). The rendering only ever keeps more of an earlier turn than the old stub did,
# and never changes it later than the old stub would have: the cached prefix is never worse off.

CONTEXT_HEAD = "[Context for the request above]"
CARRIED_NOTE = "[Unchanged - given in full in an earlier context block of this chat.]"


@dataclass(frozen=True, slots=True)
class ContextBlock:
    key: str  # which provider made it
    digest: str  # of key and body: "the same block"
    body: str  # always the full text
    carried: bool  # written as a reference when its turn was made


def block_digest(key: str, body: str) -> str:
    return hashlib.sha256(f"{key}\0{body}".encode()).hexdigest()[:20]


def _carried_text(body: str) -> str:
    """A reference: the block's headings (so "## Skill: x" still names its skill) and a note."""
    heads = [ln for ln in body.splitlines() if ln.startswith("## ")][:12]
    if not heads:
        first = body.strip().splitlines()[0] if body.strip() else ""
        heads = [first[:120]] if first else []
    return "\n".join([*heads, CARRIED_NOTE])


def compose_context(blocks: list[ContextBlock], full: list[bool]) -> str:
    parts = [b.body if f else _carried_text(b.body) for b, f in zip(blocks, full, strict=True)]
    return CONTEXT_HEAD + "\n\n" + "\n\n".join(parts)


def render_context(history: list[Message], blocks: dict[str, list[ContextBlock]], current_run_id: str) -> list[Message]:
    """The context messages of ``history`` as the model should see them now.

    In order: a carried block refers to the newest full copy of it before it, which then stays
    whole; a carried block whose copy is no longer in ``history`` (trimmed, summarised) is written
    out in full where it stands. A full copy no later turn refers to is cut to the usual stub once
    its turn is over. The current run's own blocks are always shown.
    """
    latest: dict[str, tuple[str, int]] = {}
    full: dict[tuple[str, int], bool] = {}
    needed: set[tuple[str, int]] = set()
    for m in history:
        own = blocks.get(m.id or "") if m.name == "context" else None
        if not own or m.id is None:
            continue
        for i, b in enumerate(own):
            pos = (m.id, i)
            anchor = latest.get(b.digest)
            if b.carried and anchor is not None:
                full[pos] = False
                needed.add(anchor)
            else:
                full[pos] = True
                latest[b.digest] = pos
    out: list[Message] = []
    for m in history:
        own = blocks.get(m.id or "") if m.name == "context" else None
        if not own or m.id is None:
            out.append(m)
            continue
        current = m.run_id == current_run_id
        shown: list[ContextBlock] = []
        flags: list[bool] = []
        dropped: list[ContextBlock] = []
        for i, b in enumerate(own):
            pos = (m.id, i)
            if not full[pos]:
                shown.append(b)
                flags.append(False)
            elif current or pos in needed:
                shown.append(b)
                flags.append(True)
            else:
                dropped.append(b)
        skills = [n for b in dropped for n in _skill_names(b.body)]
        if not shown:
            content = _not_repeated(skills)
        else:
            content = compose_context(shown, flags)
            if dropped:
                content += "\n\n" + _not_repeated(skills)
        out.append(m if content == m.content else m.model_copy(update={"content": content}))
    return out


def last_turn_skills(history: list[Message], current_run_id: str) -> list[str]:
    """The skills the most recent earlier turn ran with: a follow-up ("send it", "and the other
    one?") is short and names no skill, yet it is the same job and needs the same runbook."""
    for m in reversed(history):
        if m.run_id != current_run_id and m.role is Role.USER and m.name == "context":
            return _skill_names(m.content)
    return []


#: What the ledger of earlier turns may cost in the prompt, at most.
LEDGER_CHARS = 6_000
_LEDGER_ARG_KEYS = (
    "path", "filename", "name", "to", "subject", "account", "folder", "query", "q", "url", "ref",
    "entry_id", "id", "mode", "draft",
)  # fmt: skip
_LEDGER_HEAD = (
    "[Conversation ledger - every earlier turn of this chat, kept by the core and always complete, "
    "even where the turns below are shortened or summarised. It is what you already did: do not "
    "redo it, build on it. A result can be read back by its @ref with jarvis.result_read.]"
)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _call_brief(call: Any) -> str:
    """``tool(key=value, ...)`` with only the arguments that identify what was touched."""
    args = call.arguments if isinstance(call.arguments, dict) else {}
    shown = []
    for key in _LEDGER_ARG_KEYS:
        value = args.get(key)
        if value in (None, "", [], {}):
            continue
        shown.append(f"{key}={_one_line(str(value), 48)}")
        if len(shown) == 2:
            break
    return f"{call.name}({', '.join(shown)})" if shown else call.name


def _ledger_turn(index: int, turn: list[Message], *, detail: bool) -> str:
    request = next((m for m in turn if m.role is Role.USER and m.name is None), None)
    results = {m.tool_call_id: m for m in turn if m.role is Role.TOOL}
    actions: list[str] = []  # (what, ref) - consecutive repeats of the same call are folded: "x10"
    refs: list[str] = []
    counts: list[int] = []
    for m in turn:
        if m.role is not Role.ASSISTANT:
            continue
        for call in m.tool_calls:
            res = results.get(call.id)
            what = _call_brief(call) if detail else call.name
            if res is None:
                what += " (no result)"
            elif res.content.startswith("Error"):
                what += " FAILED"
            ref = f" {short_ref(call.id)}" if detail and res is not None and not res.content.startswith("Error") else ""
            if actions and actions[-1] == what:
                counts[-1] += 1
                refs[-1] = ref  # the newest of the run is the one worth reading back
            else:
                actions.append(what)
                refs.append(ref)
                counts.append(1)
    answers = [m for m in turn if m.role is Role.ASSISTANT and not m.tool_calls and m.content.strip()]
    when = (request or turn[0]).created_at.strftime("%Y-%m-%d %H:%M")
    asked = _one_line(request.content, 300 if detail else 140) if request else "(no text)"
    lines = [f"T{index} {when} - asked: {asked}"]
    if actions:
        did = "; ".join(f"{a}{r}" if n == 1 else f"{a} x{n}{r}" for a, r, n in zip(actions, refs, counts, strict=True))
        lines.append(f"  did: {_one_line(did, 900 if detail else 200)}")
    if answers:
        lines.append(f"  answered: {_one_line(answers[-1].content, 700 if detail else 160)}")
    elif any(m.partial for m in turn):
        lines.append("  answered: (interrupted before an answer)")
    return "\n".join(lines)


def conversation_ledger(history: list[Message], current_run_id: str, *, max_chars: int = LEDGER_CHARS) -> str | None:
    """A compact, deterministic record of every earlier turn: what Arsen asked, what was done
    (the tool, the arguments that say what it touched, FAILED or the result's @ref) and what was
    answered.

    The turns themselves ride only as far as the history budget reaches, and what falls out of
    it lives on in a 400-word model summary at best. That is how a follow-up after a 20-step run
    ("само прати мейла, вече имаш всичко") met a model that had to rediscover its own work - the
    file it wrote, the recipients, which host was down - and re-ran a dozen probes doing so
    (2026-10-02). This costs a few hundred characters a turn and no model call. The newest turns
    are written in detail; when the whole does not fit, the oldest shrink to one line each, and
    past that they are counted, not listed.
    """
    turns: list[list[Message]] = []
    for m in history:
        if m.run_id == current_run_id or m.role is Role.SYSTEM or m.name in ("context", "summary", "plan", "ledger"):
            continue
        if turns and m.run_id is not None and turns[-1][0].run_id == m.run_id:
            turns[-1].append(m)
        else:
            turns.append([m])
    turns = [t for t in turns if any(m.role is Role.USER or m.tool_calls for m in t)]
    if not turns:
        return None
    entries = [_ledger_turn(i + 1, t, detail=True) for i, t in enumerate(turns)]
    room = max_chars - len(_LEDGER_HEAD)

    def size() -> int:
        return sum(len(e) + 1 for e in entries)

    for i in range(len(entries) - 1):  # oldest first; the newest turn always stays detailed
        if size() <= room:
            break
        entries[i] = _ledger_turn(i + 1, turns[i], detail=False)
    dropped = 0
    while size() > room and len(entries) > 1:
        entries.pop(0)
        dropped += 1
    if dropped:
        entries.insert(0, f"({dropped} earlier turn(s) not listed - see the summary of the earlier conversation)")
    return _LEDGER_HEAD + "\n" + "\n".join(entries)


class BlockProvider(Protocol):
    """Anything that can contribute a block of text to the system message."""

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None: ...


class ContextAssembler:
    def __init__(
        self,
        store: Store,
        settings: Callable[[], Settings],
        *,
        providers: list[BlockProvider] | None = None,
        compactor: object | None = None,
        clock: Callable[[], datetime] | None = None,
        attachments: Any | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._providers: list[BlockProvider] = list(providers or [])
        self.chars_per_token = _CHARS_PER_TOKEN
        self._compactor = compactor  # features.compaction.Compactor, optional
        self._clock = clock
        self._attachments = attachments  # features.attachments.AttachmentStore, optional

    def observe(self, prompt_chars: int, prompt_tokens: int) -> None:
        """Calibrate the chars-per-token estimate from a served prompt: an exponential moving
        average, clamped, so one odd step cannot swing it and JSON-heavy runs pull it down."""
        if prompt_chars <= 0 or prompt_tokens <= 0:
            return
        seen = max(_CPT_MIN, min(_CPT_MAX, prompt_chars / prompt_tokens))
        self.chars_per_token = round(0.7 * self.chars_per_token + 0.3 * seen, 3)

    def tokens(self, chars: int) -> int:
        return int(chars / self.chars_per_token)

    def add_provider(self, provider: BlockProvider) -> None:
        self._providers.append(provider)

    @property
    def budget_chars(self) -> int:
        return int(self._settings().history_token_budget * self.chars_per_token)

    async def assemble_view(
        self,
        run: Run,
        *,
        skill_names: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
        window: int | None = None,
        max_tokens: int | None = None,
    ) -> ContextView:
        """The step's messages plus the budgets they were built to: the run's tool results are
        admitted at ``tool_result_admit_chars``, and history/results budgets are the configured
        ones unless the lane's ``window`` cannot hold them next to the system message, the tool
        schemas, the answer and the reserve (``Settings.effective_budgets``)."""
        s = self._settings()
        await self.context_message(run, skill_names=skill_names or [])  # persisted; comes back in history
        system = await self.system_message(run)
        tools_chars = tool_schema_chars(tools or [])
        fixed_tokens = self.tokens(len(system.content)) + self.tokens(tools_chars)
        history_tokens, results_tokens = s.effective_budgets(
            window=window, max_tokens=max_tokens, fixed_tokens=fixed_tokens
        )
        admit_chars = s.admit_chars(results_tokens, self.chars_per_token)
        stored = await self._store.list_messages(run.conversation_id)
        blocks = await self._blocks(run.conversation_id)
        history = earlier_turn_view(stored, run.id, admit_chars=admit_chars, structured=blocks.keys())
        # Rendered as if everything were kept, so the budget counts what will be sent ...
        history = render_context(history, blocks, run.id)
        budget_chars = int(history_tokens * self.chars_per_token)
        ledger = conversation_ledger(stored, run.id, max_chars=min(LEDGER_CHARS, budget_chars // 4))
        if ledger is not None:
            budget_chars -= len(ledger)
        if self._compactor is not None and run.kind is not RunKind.TRIAGE:
            # The stored summary only. A new one is made in the background (compact_if_due):
            # made here it cost a call 8-11 s before its first word (2026-10-13).
            history = await self._compactor.prepare(run.conversation_id, history, budget_chars)  # type: ignore[attr-defined]
            if self._compactor.cost(history) > budget_chars:  # type: ignore[attr-defined]
                self._compactor.schedule(run.conversation_id, self.compaction_view)  # type: ignore[attr-defined]
        # ... and again once trimmed: a reference whose full copy fell off is written out in place.
        kept = await self._hydrate(render_context(self.trim(history, budget_chars), blocks, run.id))
        if ledger is not None:
            # Right before this run's own messages, not at the top: it grows by a turn each turn,
            # and up there it would invalidate the cached prefix of the whole history every time.
            # Here only the new turn's tokens are new, as they are anyway.
            at = next((i for i, m in enumerate(kept) if m.run_id == run.id), len(kept))
            kept.insert(at, Message.user(ledger, name="ledger", conversation_id=run.conversation_id))
        messages = [system, *kept]
        return ContextView(
            messages=messages,
            history_tokens=history_tokens,
            results_tokens=results_tokens,
            tools_chars=tools_chars,
            window=window,
            chars_per_token=self.chars_per_token,
            admit_chars=admit_chars,
        )

    async def system_message(self, run: Run) -> Message:
        """The STABLE prefix: identical for every turn of a conversation (and every step of a run).

        Only things that change rarely live here - rules, personality, boards, the scheduled-run
        framing (constant within a run), today's date. Anything per-message goes into
        :meth:`turn_context` instead: with vLLM's prefix cache on, a system prompt that changed
        every turn re-prefilled the whole history each time (TTFT 18 s on 34k tokens, 2026-09-05).
        """
        s = self._settings()
        parts = [
            SYSTEM_RULES.format(assistant_name=s.assistant_name, user_name=s.user_name, language_hint=s.language_hint)
        ]
        if (voice := personality_block(s.personality, s.user_name, s.assistant_name)) is not None:
            parts.append(voice)
        for provider in self._providers:
            if not getattr(provider, "stable", False):
                continue
            try:
                block = await provider.context_block(run, skill_names=[])
            except Exception:
                block = None
            if block:
                parts.append(block)
        # This chat's own instructions, if it has any. LAST of the stable blocks on purpose: a
        # conversation without them is byte-identical to every other, and one with them still
        # shares the cached prefix up to this point, so only the tail is re-read.
        conversation = await self._store.get_conversation(run.conversation_id)
        if conversation is not None and conversation.instructions.strip():
            parts.append(
                "## Instructions for this conversation\n"
                "These come from Arsen and apply to this chat only. They add to the rules above "
                "and never override rule 1 or 2.\n" + conversation.instructions.strip()
            )
        if conversation is not None and conversation.id == s.assistant.home_conversation_id:
            # The Jarvis conversation (docs/stories/12_assistant.md): the one chat that is about
            # his day rather than one task. Constant for this conversation, so it caches.
            parts.append(
                f"## The {s.assistant_name} conversation\n"
                f"This is {s.user_name}'s main line to you, not a chat about one task: how his day is going, "
                "what is waiting on him, what to remember or remind him of, and anything he thinks of. Every "
                "turn carries where his other conversations stand today; when he asks about one of them, "
                "read it (sessions.read) before you answer in detail."
            )
        if conversation is not None and conversation.incognito:
            # The code already keeps the learner, the titler and the memory tools away from this
            # chat; this block is so the model neither promises to remember something it cannot,
            # nor disowns what it does know. Reads stay open on purpose (Arsen: incognito must have
            # all the knowledge); the first wording made the model call a reservation it had just
            # read off his boards a hallucination.
            parts.append(
                "## Private conversation\n"
                "This is an incognito chat. You still know everything you normally know: the boards "
                "and known context above are yours to use, and so are the tools. What changes is that "
                "nothing NEW from this chat is kept anywhere - it is never learned, and you have no "
                f"notes or knowledge tools here. If {s.user_name} asks you to remember or save "
                "something, say that this chat cannot and an ordinary chat can. If asked how you know "
                "something, the answer is the boards or the known context above - never say you "
                "guessed or hallucinated when you did not."
            )
        if run.kind is RunKind.SCHEDULED:
            # The model must know it IS the reminder. Without this, "Remind Arsen to ..." firing
            # at 08:45 was read as "set up a reminder" and it created a second schedule.
            name = conversation.folder_label if conversation and conversation.folder_label else "scheduled prompt"
            parts.append(
                f"## This run\nYou are executing the scheduled prompt '{name}' which is firing NOW. "
                "Do what it says immediately, in this run. Do not create, edit or re-schedule any "
                "schedule for it - it already exists and is what triggered you. Your final reply IS "
                "the reminder/report; Arsen reads it in the Scheduled folder, so write it for him. "
                "Nobody is present to answer questions: decide, act within the prompt's limits, and report."
            )
        parts.append(self._date_line(s))
        return Message.system("\n\n".join(parts))

    async def turn_blocks(self, run: Run, *, skill_names: list[str]) -> list[tuple[str, str]]:
        """Per-turn context (skills, knowledge, browser page, the call's style, the day) as
        (key, text) blocks - constant within a run.

        Persisted as a user message named "context" right after the run's input, so every later
        turn replays the same tokens at the same position and the prefix cache holds.
        """
        out: list[tuple[str, str]] = []
        for provider in self._providers:
            if getattr(provider, "stable", False):
                continue
            try:
                block = await provider.context_block(run, skill_names=skill_names)
            except Exception:
                block = None
            if block:
                out.append((type(provider).__name__, block))
        return out

    async def turn_context(self, run: Run, *, skill_names: list[str]) -> str | None:
        """The per-turn context as one text, every block in full."""
        blocks = await self.turn_blocks(run, skill_names=skill_names)
        return CONTEXT_HEAD + "\n\n" + "\n\n".join(text for _k, text in blocks) if blocks else None

    async def _blocks(self, conversation_id: str) -> dict[str, list[ContextBlock]]:
        rows = await self._store.context_blocks(conversation_id)
        return {mid: [ContextBlock(k, d, b, c) for k, d, b, c in blocks] for mid, blocks in rows.items()}

    async def compaction_view(self, conversation_id: str) -> tuple[list[Message], int]:
        """The conversation as its next turn will see it before trimming, and that turn's history
        budget in chars: what the background compactor folds (features/compaction.py)."""
        stored = await self._store.list_messages(conversation_id)
        blocks = await self._blocks(conversation_id)
        history = render_context(earlier_turn_view(stored, "", structured=blocks.keys()), blocks, "")
        budget = int(self._settings().history_token_budget * self.chars_per_token)
        ledger = conversation_ledger(stored, "", max_chars=min(LEDGER_CHARS, budget // 4))
        return history, budget - (len(ledger) if ledger else 0)

    async def compact_if_due(self, conversation_id: str) -> None:
        """After a run: fold the older history into the summary in the background once it nears
        its budget, so the next turns find it done instead of waiting for it."""
        if self._compactor is not None:
            self._compactor.schedule(conversation_id, self.compaction_view, early=True)  # type: ignore[attr-defined]

    @staticmethod
    def plan_message(plan: Plan) -> Message:
        """Ephemeral trailer: plan progress changes every step, so it goes LAST, after all the
        cached tool results, instead of invalidating them from the system prompt."""
        from jarvis_core.features.planner import plan_block

        return Message.user(plan_block(plan), name="plan")

    async def context_message(self, run: Run, *, skill_names: list[str]) -> Message | None:
        """Persist (once) and return this run's context message, or None when there is nothing."""
        existing = [m for m in await self._store.list_run_messages(run.id) if m.name == "context"]
        if existing:
            return existing[0]
        made = await self.turn_blocks(run, skill_names=skill_names)
        if not made:
            return None
        # A block this conversation has been given before is written as a reference to it.
        known = {b.digest for blocks in (await self._blocks(run.conversation_id)).values() for b in blocks}
        blocks: list[ContextBlock] = []
        for key, body in made:
            digest = block_digest(key, body)
            blocks.append(ContextBlock(key, digest, body, carried=digest in known))
            known.add(digest)
        text = compose_context(blocks, [not b.carried for b in blocks])
        msg = Message.user(text, name="context", conversation_id=run.conversation_id, run_id=run.id)
        await self._store.add_message(msg)
        assert msg.id is not None
        await self._store.add_context_blocks(msg.id, [(b.key, b.digest, b.body, b.carried) for b in blocks])
        return msg

    def system_prompt(self) -> Message:
        """Static prompt only (used by tests and by callers without a run)."""
        s = self._settings()
        return Message.system(
            SYSTEM_RULES.format(assistant_name=s.assistant_name, user_name=s.user_name, language_hint=s.language_hint)
            + "\n\n"
            + self._date_line(s)
        )

    def _date_line(self, s: Settings) -> str:
        tz = ZoneInfo(s.timezone)
        now = self._clock() if self._clock else datetime.now(tz)
        return f"Current date: {now.astimezone(tz).strftime('%A %Y-%m-%d')} ({s.timezone})."

    async def assemble(self, run: Run, *, skill_names: list[str] | None = None) -> list[Message]:
        """[stable system] [history incl. earlier turns' context messages] [input] [this run's context]."""
        return (await self.assemble_view(run, skill_names=skill_names)).messages

    async def _hydrate(self, history: list[Message]) -> list[Message]:
        """Fill in what the model needs from attachments: image bytes as data URLs, and a line
        naming the files a message carried. Done here, on copies, so nothing persisted grows."""
        if self._attachments is None:
            return history
        ids = [m.id for m in history if m.id]
        by_message = await self._attachments.for_messages(ids)
        if not by_message:
            return history
        # Pictures and clips ride as pixels only for the newest few (settings.media_in_context):
        # every vLLM has a per-prompt limit on them (the syv-ai image ships 1, raised to 6 on
        # both lanes) and refuses the whole request past it — a chat with two photos in its
        # history died with "At most 1 image(s) may be provided" (2026-09-17). Older ones keep
        # their note, saying they were shown earlier and can be shown again on request.
        budget = self._settings().media_in_context
        shown_ids: set[str] = set()
        for m in reversed(history):
            for att in by_message.get(m.id or "", []):
                outgoing = bool(att.meta.get("outgoing"))
                if (
                    att.kind in (AttachmentKind.IMAGE, AttachmentKind.VIDEO)
                    and not outgoing
                    and len(shown_ids) < budget
                ):
                    shown_ids.add(att.id)
        out: list[Message] = []
        for m in history:
            atts = by_message.get(m.id or "", [])
            if not atts:
                out.append(m)
                continue
            hydrated = []
            for att in atts:
                url = await self._attachments.data_url(att.id) if att.id in shown_ids else None
                hydrated.append(att.model_copy(update={"data_url": url}) if url else att)
            out.append(
                m.model_copy(
                    update={"attachments": hydrated, "content": _with_attachment_note(m, hydrated, shown_earlier=True)}
                )
            )
        return out

    def trim(self, history: list[Message], budget_chars: int | None = None) -> list[Message]:
        """Newest messages that fit the budget, cut at a user-message boundary so no tool
        message is ever orphaned from the assistant call that produced it."""
        # The compaction summary is pinned: it is the memory of everything trimmed away.
        pinned = [m for m in history if m.name == "summary"]
        rest = [m for m in history if m.name != "summary"]
        kept: list[Message] = []
        used = sum(len(m.content) + 8 for m in pinned)
        budget = budget_chars if budget_chars is not None else self.budget_chars
        for m in reversed(rest):
            cost = len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) + 8
            if used + cost > budget and kept:
                break
            kept.append(m)
            used += cost
        kept.reverse()
        while kept and kept[0].role is not Role.USER:
            kept.pop(0)
        return [*pinned, *kept]


@dataclass
class ContextView:
    """What one step was built from: the messages and the budgets that shaped them."""

    messages: list[Message]
    history_tokens: int
    results_tokens: int
    tools_chars: int
    window: int | None
    chars_per_token: float = _CHARS_PER_TOKEN
    admit_chars: int = 48_000


def prompt_chars(messages: list[Message], tools_chars: int) -> int:
    """What a step sends, in chars: the calibration's numerator."""
    return tools_chars + sum(len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) for m in messages)


def tool_schema_chars(tools: list[ToolSpec]) -> int:
    """How much of the prompt the tool list is, roughly: what the chat template renders per tool."""
    import json

    return sum(len(t.name) + len(t.description) + len(json.dumps(t.input_schema)) + 16 for t in tools)


def context_breakdown(
    messages: list[Message],
    run_id: str,
    *,
    tools_chars: int,
    window: int | None,
    chars_per_token: float = _CHARS_PER_TOKEN,
) -> ContextBreakdown:
    """Where a step's prompt goes, by part. ``messages[0]`` is the system message; this run's own
    tool results count as results, everything else after the system message as history."""

    def _tokens(chars: int) -> int:
        return int(chars / chars_per_token)

    system = len(messages[0].content) if messages else 0
    history = results = admitted = 0
    for m in messages[1:]:
        cost = len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls)
        if m.run_id == run_id and m.role is Role.TOOL:
            results += cost
            admitted += 1 if is_admitted_head(m) else 0
        else:
            history += cost
    parts = [_tokens(n) for n in (system, tools_chars, history, results)]
    return ContextBreakdown(
        system=parts[0],
        tools=parts[1],
        history=parts[2],
        results=parts[3],
        total=sum(parts),
        window=window,
        admitted=admitted,
    )


def _with_attachment_note(message: Message, attachments: list[Attachment], *, shown_earlier: bool = False) -> str:
    """The message text plus what came with it. Images say they are shown; text is inlined here
    so a document reaches even a model that cannot see pictures. ``shown_earlier``: a picture
    without pixels here was shown in an earlier turn and is only past the media budget."""
    parts = [message.content] if message.content else []
    unseen = (
        " — shown earlier in this conversation, not repeated here; ask to see it again if needed"
        if shown_earlier
        else " — this model cannot be shown images, so describe what you need instead"
    )
    for att in attachments:
        size = f"{att.bytes // 1024} kB" if att.bytes >= 1024 else f"{att.bytes} B"
        if att.meta.get("outgoing"):
            parts.append(f"[you attached {att.name} ({size}) to this reply; Arsen downloads it from the chat]")
            continue
        if att.kind is AttachmentKind.IMAGE:
            note = f"[image attached: {att.name}, {size}"
            if not att.data_url:
                note += unseen
            parts.append(note + "]")
        elif att.kind is AttachmentKind.VIDEO:
            # What it was sampled to is part of the content: an answer about "the whole video"
            # is only as good as the frames, and the model should know what it is looking at.
            # And whether it HEARD it: the frames carry no sound, so the transcript is the only
            # way what was said in the clip reaches the model — or the reason it did not.
            shape = att.meta.get("sampled") or f"{att.meta.get('frames', '?')} frames"
            note = f"[video attached: {att.name}, {size}, shown as {shape}"
            if not att.data_url:
                note += unseen.replace("images", "video")
            audio = str(att.meta.get("audio") or "")
            if att.text:
                lang = f" ({att.meta['language']})" if att.meta.get("language") else ""
                parts.append(note + f"; what is said in it{lang}:]\n{att.text}")
            elif audio == "none":
                parts.append(note + "; it has no audio track]")
            elif audio == "silent":
                parts.append(note + "; nothing was said in it]")
            else:
                why = audio or "audio not transcribed"
                parts.append(note + f"; its sound was NOT heard ({why}) — say so if it matters]")
        elif att.text:
            label = "email thread" if att.kind is AttachmentKind.EMAIL else att.name
            parts.append(f"[attached {label} ({size})]\n{att.text}")
        else:
            parts.append(f"[attached file: {att.name}, {size} — no text could be read from it]")
    return "\n\n".join(parts)


# --- ready-made providers ---------------------------------------------------------------


class BoardsBlock:
    stable = True  # changes only when Arsen pins a note: lives in the cached system prefix

    def __init__(self, boards: object, settings: Callable[[], Settings]) -> None:
        self._boards = boards
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        return await self._boards.context_block(self._settings().boards_context_chars)  # type: ignore[attr-defined]


class SkillsBlock:
    def __init__(self, detector: object, settings: Callable[[], Settings]) -> None:
        self._detector = detector
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if not skill_names:
            return None
        return await self._detector.context_block(skill_names, max_chars=self._settings().skill_max_chars)  # type: ignore[attr-defined]


class KnowledgeBlock:
    def __init__(self, knowledge: object) -> None:
        self._knowledge = knowledge

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        return await self._knowledge.context_block(run.input_text)  # type: ignore[attr-defined]


class VoiceBlock:
    """The "you are on a call" instructions, for voice runs only.

    Per-turn context, never the stable prefix: the prefix is cached across every run of every
    conversation, and a block that is there for one turn in ten would invalidate it for the
    other nine. Here it costs the same tokens as a skill and rides at the same place.
    """

    def __init__(self, settings: Callable[[], Settings]) -> None:
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if run.channel is not Channel.VOICE:
            return None
        style = self._settings().voice.style.strip()
        return "## Voice call\n" + style if style else None


class BrowserBlock:
    def __init__(self, browser: object) -> None:
        self._browser = browser

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if run.kind is not RunKind.CHAT:
            return None
        return self._browser.context_block()  # type: ignore[attr-defined]


class SessionsBlock:
    """Who @name means, but only on a turn that says @something.

    Arsen addresses another chat the way he addresses anyone — "@домо питай ги кога е
    събранието". The model can find the handles with sessions.list, but that is a round trip for
    something he has already written down, so the handles ride with the turn that mentions one.
    Only then: the block is per-turn context and the ninety-nine turns with no @ in them must not
    pay for it (the same rule as VoiceBlock).
    """

    def __init__(self, sessions: object) -> None:
        self._sessions = sessions

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if "@" not in run.input_text:
            return None
        listed = await self._sessions.handles_block(exclude=run.conversation_id)  # type: ignore[attr-defined]
        return f"## Sessions you can address{chr(10)}{listed}" if listed else None
