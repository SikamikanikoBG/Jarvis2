"""Context assembly: one system message built from providers in a cache-friendly order, then
the (compacted, trimmed) history.

Order: static rules → boards (change rarely) → skills (per message) → knowledge (per message)
→ browser page (per message) → plan (per step) → date. Nothing here calls a model except
the compactor, and only when the history has outgrown its budget.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from jarvis_core.db import Store
from jarvis_core.features.personality import personality_block
from jarvis_proto import Attachment, AttachmentKind, Message, Plan, Role, Run, RunKind, Settings

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
6. When a tool result says [partial], page through it before concluding.
7. When a tool fails, say so plainly and try one sensible alternative, not the same call.
8. Format for a phone screen: short paragraphs, lists only when they add clarity.
9. Notes boards and known context below are facts Arsen curated; prefer them over guesses,
   and use notes.add when Arsen asks you to remember something."""

_CHARS_PER_TOKEN = 3.2


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
        self._compactor = compactor  # features.compaction.Compactor, optional
        self._clock = clock
        self._attachments = attachments  # features.attachments.AttachmentStore, optional

    def add_provider(self, provider: BlockProvider) -> None:
        self._providers.append(provider)

    @property
    def budget_chars(self) -> int:
        return int(self._settings().history_token_budget * _CHARS_PER_TOKEN)

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

    async def turn_context(self, run: Run, *, skill_names: list[str]) -> str | None:
        """Per-turn context (skills, knowledge, browser page) - constant within a run.

        It is persisted as a user message named "context" right after the run's input, so every
        later turn replays the exact same tokens at the same position and the prefix cache holds.
        """
        parts: list[str] = []
        for provider in self._providers:
            if getattr(provider, "stable", False):
                continue
            try:
                block = await provider.context_block(run, skill_names=skill_names)
            except Exception:
                block = None
            if block:
                parts.append(block)
        return "[Context for the request above]\n\n" + "\n\n".join(parts) if parts else None

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
        text = await self.turn_context(run, skill_names=skill_names)
        if text is None:
            return None
        msg = Message.user(text, name="context", conversation_id=run.conversation_id, run_id=run.id)
        await self._store.add_message(msg)
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
        await self.context_message(run, skill_names=skill_names or [])  # persisted; comes back in history
        history = await self._store.list_messages(run.conversation_id)
        if self._compactor is not None and run.kind is not RunKind.TRIAGE:
            history = await self._compactor.prepare(run.conversation_id, history, self.budget_chars)  # type: ignore[attr-defined]
        system = await self.system_message(run)
        return [system, *await self._hydrate(self.trim(history))]

    async def _hydrate(self, history: list[Message]) -> list[Message]:
        """Fill in what the model needs from attachments: image bytes as data URLs, and a line
        naming the files a message carried. Done here, on copies, so nothing persisted grows."""
        if self._attachments is None:
            return history
        ids = [m.id for m in history if m.id]
        by_message = await self._attachments.for_messages(ids)
        if not by_message:
            return history
        out: list[Message] = []
        for m in history:
            atts = by_message.get(m.id or "", [])
            if not atts:
                out.append(m)
                continue
            hydrated = []
            for att in atts:
                url = await self._attachments.data_url(att.id) if att.kind is AttachmentKind.IMAGE else None
                hydrated.append(att.model_copy(update={"data_url": url}) if url else att)
            out.append(m.model_copy(update={"attachments": hydrated, "content": _with_attachment_note(m, hydrated)}))
        return out

    def trim(self, history: list[Message]) -> list[Message]:
        """Newest messages that fit the budget, cut at a user-message boundary so no tool
        message is ever orphaned from the assistant call that produced it."""
        # The compaction summary is pinned: it is the memory of everything trimmed away.
        pinned = [m for m in history if m.name == "summary"]
        rest = [m for m in history if m.name != "summary"]
        kept: list[Message] = []
        used = sum(len(m.content) + 8 for m in pinned)
        budget = self.budget_chars
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


def _with_attachment_note(message: Message, attachments: list[Attachment]) -> str:
    """The message text plus what came with it. Images say they are shown; text is inlined here
    so a document reaches even a model that cannot see pictures."""
    parts = [message.content] if message.content else []
    for att in attachments:
        size = f"{att.bytes // 1024} kB" if att.bytes >= 1024 else f"{att.bytes} B"
        if att.kind is AttachmentKind.IMAGE:
            note = f"[image attached: {att.name}, {size}"
            if not att.data_url:
                note += " — this model cannot be shown images, so describe what you need instead"
            parts.append(note + "]")
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


class BrowserBlock:
    def __init__(self, browser: object) -> None:
        self._browser = browser

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if run.kind is not RunKind.CHAT:
            return None
        return self._browser.context_block()  # type: ignore[attr-defined]
