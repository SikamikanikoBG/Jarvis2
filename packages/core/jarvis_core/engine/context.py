"""Context assembly: static system prompt (cache-friendly prefix) + bounded history.

Phase 2 adds providers (boards, skills, KG, compaction). Nothing here calls a model.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from jarvis_core.db import Store
from jarvis_proto import Message, Role, Run, Settings

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

Current date: {date} ({timezone})."""

# Rough chars-per-token for a mixed BG/EN prompt; history is trimmed by this estimate.
_CHARS_PER_TOKEN = 3.2


class ContextAssembler:
    def __init__(
        self,
        store: Store,
        settings: Callable[[], Settings],
        *,
        history_token_budget: int = 24_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._budget_chars = int(history_token_budget * _CHARS_PER_TOKEN)
        self._clock = clock

    def system_prompt(self) -> Message:
        s = self._settings()
        tz = ZoneInfo(s.timezone)
        now = self._clock() if self._clock else datetime.now(tz)
        return Message.system(
            SYSTEM_RULES.format(
                assistant_name=s.assistant_name,
                user_name=s.user_name,
                language_hint=s.language_hint,
                date=now.astimezone(tz).strftime("%A %Y-%m-%d"),
                timezone=s.timezone,
            )
        )

    async def assemble(self, run: Run) -> list[Message]:
        history = await self._store.list_messages(run.conversation_id)
        return [self.system_prompt(), *self.trim(history)]

    def trim(self, history: list[Message]) -> list[Message]:
        """Newest messages that fit the budget, cut at a user-message boundary so no tool
        message is ever orphaned from the assistant call that produced it."""
        kept: list[Message] = []
        used = 0
        for m in reversed(history):
            cost = len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) + 8
            if used + cost > self._budget_chars and kept:
                break
            kept.append(m)
            used += cost
        kept.reverse()
        # Drop leading non-user messages (a tool result without its call, etc.).
        while kept and kept[0].role is not Role.USER:
            kept.pop(0)
        return kept
