"""Context assembly: one system message built from providers in a cache-friendly order, then
the (compacted, trimmed) history.

Order: static rules → boards (change rarely) → skills (per message) → knowledge (per message)
→ browser page (per message) → plan (per step) → date. Nothing here calls a model except
the compactor, and only when the history has outgrown its budget.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from jarvis_core.db import Store
from jarvis_proto import Message, Plan, Role, Run, RunKind, Settings

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
    ) -> None:
        self._store = store
        self._settings = settings
        self._providers: list[BlockProvider] = list(providers or [])
        self._compactor = compactor  # features.compaction.Compactor, optional
        self._clock = clock

    def add_provider(self, provider: BlockProvider) -> None:
        self._providers.append(provider)

    @property
    def budget_chars(self) -> int:
        return int(self._settings().history_token_budget * _CHARS_PER_TOKEN)

    async def system_message(self, run: Run, *, skill_names: list[str], plan: Plan | None) -> Message:
        s = self._settings()
        parts = [
            SYSTEM_RULES.format(assistant_name=s.assistant_name, user_name=s.user_name, language_hint=s.language_hint)
        ]
        for provider in self._providers:
            try:
                block = await provider.context_block(run, skill_names=skill_names)
            except Exception:
                block = None
            if block:
                parts.append(block)
        if plan is not None:
            from jarvis_core.features.planner import plan_block

            parts.append(plan_block(plan))
        parts.append(self._date_line(s))
        return Message.system("\n\n".join(parts))

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

    async def assemble(
        self, run: Run, *, skill_names: list[str] | None = None, plan: Plan | None = None
    ) -> list[Message]:
        history = await self._store.list_messages(run.conversation_id)
        if self._compactor is not None and run.kind is not RunKind.TRIAGE:
            history = await self._compactor.prepare(run.conversation_id, history, self.budget_chars)  # type: ignore[attr-defined]
        system = await self.system_message(run, skill_names=skill_names or [], plan=plan)
        return [system, *self.trim(history)]

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


# --- ready-made providers ---------------------------------------------------------------


class BoardsBlock:
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
