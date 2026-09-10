"""Disappearing chats: the sweep that deletes them once their idle time is up.

A chat with ``ttl_seconds`` carries ``expires_at`` (its last message plus the ttl; the store
maintains it). Every minute this asks the store which of those are due, and removes them the
way a Delete from the sidebar does - runs cancelled, attachment files unlinked, the row gone
(messages, runs, events and summaries cascade), one ``conversation.deleted`` on the bus so every
open client drops the row. A chat with a run still working is left for the next round: the
reply that is about to land will push its expiry out again anyway.

An incognito chat is only swept if Arsen gave it a timer too: incognito is about what is
remembered, not about how long the chat stays.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from jarvis_proto import TTL_CHOICES
from jarvis_proto.events import ConversationDeleted

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 60.0


def valid_ttl(ttl_seconds: int | None) -> int | None:
    """The ttl if it is one Arsen can pick (an hour, a day, a week), else None. A ttl is a
    promise about when something is gone, so an arbitrary number - 0, a negative, ten years -
    is refused rather than rounded."""
    return ttl_seconds if ttl_seconds in TTL_CHOICES else None


class Reaper:
    def __init__(self, core: Core, *, interval_s: float = SWEEP_INTERVAL_S) -> None:
        self.core = core
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="chat-reaper")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.sweep()
            except Exception:
                log.exception("chat reaper sweep failed")

    async def sweep(self) -> list[str]:
        """Delete every disappearing chat whose time is up. Returns the ids removed."""
        removed: list[str] = []
        for conv in await self.core.store.expired_conversations():
            runs = await self.core.store.list_runs(conv.id, limit=10)
            if any(not r.status.terminal for r in runs):
                continue  # still working; its reply will re-arm the timer
            await self.core.delete_conversation(conv.id)
            removed.append(conv.id)
            log.info(
                "chat %s expired after %ss idle (%s)",
                conv.id,
                conv.ttl_seconds,
                "incognito" if conv.incognito else "disappearing",
            )
        return removed


async def delete_conversation(core: Core, conversation_id: str) -> None:
    """The one way a conversation leaves: cancel what is running in it, unlink its attachment
    files (the rows cascade, the files do not), delete the row, tell every client."""
    for run in await core.store.list_runs(conversation_id):
        if not run.status.terminal:
            await core.engine.cancel(run.id)
    await core.attachments.delete_for_conversation(conversation_id)
    await core.store.delete_conversation(conversation_id)
    core.bus.publish(ConversationDeleted(conversation_id=conversation_id))
