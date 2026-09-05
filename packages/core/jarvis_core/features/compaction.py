"""Conversation compaction: older turns become one summary message when the history outgrows
the working budget. The summary is persisted so it is stable across turns (prefix-cache friendly)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from jarvis_core.db import Database
from jarvis_core.models.base import ModelAdapter, ModelTextChunk
from jarvis_proto import Message, Role, new_id

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = """Summarise the conversation below for the assistant's own memory. Keep: facts stated,
decisions, names, numbers, open tasks and what the user still expects. Drop pleasantries and
anything superseded. Write compact bullet points, max 400 words. If a previous summary is given,
merge it.

PREVIOUS SUMMARY:
{previous}

CONVERSATION:
{conversation}"""


class Compactor:
    def __init__(self, db: Database, classifier: Any) -> None:
        self.db = db
        self._classifier = classifier  # Callable[[], ModelAdapter]

    async def latest(self, conversation_id: str) -> tuple[str, str] | None:
        row = await self.db.fetchone(
            "SELECT up_to_message_id, text FROM conversation_summaries WHERE conversation_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (conversation_id,),
        )
        return (row["up_to_message_id"], row["text"]) if row else None

    async def save(self, conversation_id: str, up_to_message_id: str, text: str) -> None:
        await self.db.execute(
            "INSERT INTO conversation_summaries(id, conversation_id, up_to_message_id, text, created_at) VALUES (?,?,?,?,?)",
            (new_id("sum"), conversation_id, up_to_message_id, text, datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def cost(messages: list[Message]) -> int:
        return sum(len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) + 8 for m in messages)

    async def prepare(self, conversation_id: str, history: list[Message], budget_chars: int) -> list[Message]:
        """History with the stored summary applied and, if still too long, a fresh one made."""
        latest = await self.latest(conversation_id)
        summary_text: str | None = None
        if latest is not None:
            up_to, summary_text = latest
            idx = next((i for i, m in enumerate(history) if m.id == up_to), None)
            if idx is not None:
                history = history[idx + 1 :]
        if self.cost(history) > budget_chars and len(history) > 4:
            cut = _cut_index(history)
            if cut > 0:
                older, history = history[:cut], history[cut:]
                new_summary = await self._summarise(summary_text, older)
                if new_summary and older[-1].id:
                    await self.save(conversation_id, older[-1].id, new_summary)
                    summary_text = new_summary
                else:
                    history = older + history  # summarisation failed: keep everything, trim later
        out: list[Message] = []
        if summary_text:
            out.append(Message.user(f"[Summary of the earlier conversation]\n{summary_text}", name="summary"))
        out.extend(history)
        return out

    async def _summarise(self, previous: str | None, older: list[Message]) -> str | None:
        lines: list[str] = []
        for m in older:
            if m.role is Role.TOOL:
                lines.append(f"TOOL {m.name}: {m.content[:300]}")
            elif m.role is Role.ASSISTANT and m.tool_calls:
                lines.append(f"ASSISTANT called {', '.join(c.name for c in m.tool_calls)}. {m.content[:300]}")
            else:
                lines.append(f"{m.role.value.upper()}: {m.content[:1200]}")
        prompt = _SUMMARY_PROMPT.format(previous=previous or "(none)", conversation="\n".join(lines)[:24_000])
        try:
            adapter: ModelAdapter = self._classifier()
            text = ""
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
            return text.strip() or None
        except Exception as exc:
            log.warning("compaction skipped: %s", exc)
            return None


def _cut_index(history: list[Message]) -> int:
    """Index of a user message near the middle so no tool result is separated from its call."""
    target = len(history) // 2
    for i in range(target, 0, -1):
        if history[i].role is Role.USER and history[i].name != "supervisor":
            return i
    return 0
