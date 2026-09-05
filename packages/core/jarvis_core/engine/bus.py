"""In-process event fan-out. Run-scoped events go to subscribers of that conversation;
conversation-level events go to everyone. A subscriber that cannot keep up is dropped,
never allowed to stall the engine."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis_proto.events import ConversationDeleted, ConversationUpdated, MessageCreated, RunEvent, RunUpdated

log = logging.getLogger(__name__)

QUEUE_LIMIT = 2000


class Subscriber:
    def __init__(self, name: str = "ws") -> None:
        self.name = name
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.conversations: set[str] = set()
        self.dead = False

    def deliver(self, event: Any) -> None:
        if self.dead:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dead = True
            log.warning("subscriber %s dropped: queue full (slow consumer)", self.name)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self.published = 0

    def attach(self, sub: Subscriber) -> None:
        self._subs.add(sub)

    def detach(self, sub: Subscriber) -> None:
        self._subs.discard(sub)
        sub.dead = True

    def has_subscribers(self, conversation_id: str) -> bool:
        return any(conversation_id in s.conversations and not s.dead for s in self._subs)

    def publish(self, event: Any) -> None:
        self.published += 1
        conv: str | None
        if isinstance(event, RunEvent):
            conv = event.conversation_id
        elif isinstance(event, MessageCreated):
            conv = event.message.conversation_id
        elif isinstance(event, RunUpdated):
            conv = event.run.conversation_id
        elif isinstance(event, ConversationUpdated | ConversationDeleted):
            conv = None
        else:
            conv = None
        for sub in list(self._subs):
            if sub.dead:
                self._subs.discard(sub)
                continue
            if conv is None or conv in sub.conversations:
                sub.deliver(event)
