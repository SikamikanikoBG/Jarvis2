"""Conversation compaction: older turns become one summary message when the history outgrows
the working budget. The summary is persisted so it is stable across turns (prefix-cache friendly).

Never in a reply's way (docs/stories/12_assistant.md). A turn only applies the summary already
stored; making a new one happens in the background - after a run, once the history is close to its
budget, and when a turn finds it over. It used to happen inside the turn: on 2026-10-13 a call in a
180-message chat waited 8-11 s for a summary before each of two answers, and each new summary
then invalidated the cached prefix (time to first token 21 s). And the cut used to be the middle of
the history, which in a chat with one long run left the rest still over budget, so the next turn
summarised again. Now the cut is deep enough that what remains is at most half the budget: the
next several turns share one summary and one cached prefix.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from jarvis_core.db import Database
from jarvis_core.models.base import BACKGROUND, ModelAdapter, ModelTextChunk, call_urgency
from jarvis_proto import Message, Role, new_id

log = logging.getLogger(__name__)

#: After a run, a summary is made once the history passes this share of its budget...
EARLY_SHARE = 0.8
#: ... and it folds enough that what is left is at most this share.
KEEP_SHARE = 0.5

_SUMMARY_PROMPT = """Summarise the conversation below for the assistant's own memory. Keep: facts stated,
decisions, names, numbers, open tasks and what the user still expects. Drop pleasantries and
anything superseded. Write compact bullet points, max 400 words. If a previous summary is given,
merge it.

PREVIOUS SUMMARY:
{previous}

CONVERSATION:
{conversation}"""

#: The conversation as its next turn will see it, and its history budget in chars.
View = Callable[[str], Awaitable[tuple[list[Message], int]]]


class Compactor:
    def __init__(self, db: Database, model: Any) -> None:
        self.db = db
        self._model = model  # Callable[[], ModelAdapter]: the housekeeping lane, not the reply's
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._again: set[str] = set()

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
        """History with the stored summary applied: the summary first, then what came after it.
        No model call - a new summary is ``schedule``'s business."""
        del budget_chars  # whatever is still over it is the trim's
        summary_text, history = await self._applied(conversation_id, history)
        out: list[Message] = []
        if summary_text:
            out.append(Message.user(f"[Summary of the earlier conversation]\n{summary_text}", name="summary"))
        out.extend(history)
        return out

    async def _applied(self, conversation_id: str, history: list[Message]) -> tuple[str | None, list[Message]]:
        latest = await self.latest(conversation_id)
        if latest is None:
            return None, history
        up_to, text = latest
        idx = next((i for i, m in enumerate(history) if m.id == up_to), None)
        return text, history[idx + 1 :] if idx is not None else history

    # --- in the background -----------------------------------------------------------------

    def schedule(self, conversation_id: str, view: View, *, early: bool = False) -> None:
        """Make a new summary for this conversation in the background if it needs one: past its
        budget, or (``early``, after a run) past EARLY_SHARE of it. One at a time per
        conversation; a request while one is being made is looked at again once it is done."""
        task = self._tasks.get(conversation_id)
        if task is not None and not task.done():
            self._again.add(conversation_id)
            return
        task = asyncio.create_task(self._passes(conversation_id, view, early), name=f"compact:{conversation_id}")
        self._tasks[conversation_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(conversation_id, None))

    async def _passes(self, conversation_id: str, view: View, early: bool) -> None:
        call_urgency.set(BACKGROUND)  # not the urgency of the turn that asked for it
        while True:
            await self._compact(conversation_id, view, early)
            if conversation_id not in self._again:
                return
            # Asked again while this pass ran (a run ended meanwhile): look once more, now.
            self._again.discard(conversation_id)
            early = True

    async def stop(self) -> None:
        self._again.clear()
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def wait(self) -> None:
        """Until no summary is being made (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def _compact(self, conversation_id: str, view: View, early: bool) -> None:
        try:
            history, budget = await view(conversation_id)
            previous, rest = await self._applied(conversation_id, history)
            if self.cost(rest) <= budget * (EARLY_SHARE if early else 1.0):
                return
            cut = fold_point(rest, int(budget * KEEP_SHARE))
            if cut <= 0:
                return  # one long turn: nothing older to fold; the trim deals with it
            older = rest[:cut]
            up_to = older[-1].id
            if not up_to:
                return
            text = await self._summarise(previous, older)
            if text:
                await self.save(conversation_id, up_to, text)
                log.info("compacted %s: %d messages folded into the summary", conversation_id, len(older))
        except Exception as exc:  # housekeeping: it never fails anything
            log.warning("compaction of %s skipped: %s", conversation_id, exc)

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
        adapter: ModelAdapter = self._model()
        text = ""
        async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
            if isinstance(chunk, ModelTextChunk):
                text += chunk.text
        return text.strip() or None


def fold_point(history: list[Message], keep_chars: int) -> int:
    """Where to cut: the earliest message ARSEN wrote from which the rest costs at most
    ``keep_chars``, so a turn is folded whole and what stays leaves room to grow. When even the
    newest turn costs more, the cut is at the newest turn and everything before it is folded.
    0 means there is nothing older to fold (a conversation of one turn).

    Only his own messages qualify (``name`` is None): the context block the core injects right
    after each of them is a user-role message too, and cutting there summarised his question and
    left its answer to be trimmed (2026-09-11 - a 340-character summary of "find the mail", and
    the mail hunted for again next turn).
    """
    starts = [i for i, m in enumerate(history) if i > 0 and m.role is Role.USER and m.name is None]
    if not starts:
        return 0
    from_here = [0] * (len(history) + 1)
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        from_here[i] = from_here[i + 1] + len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) + 8
    fitting = [i for i in starts if from_here[i] <= keep_chars]
    return fitting[0] if fitting else starts[-1]
