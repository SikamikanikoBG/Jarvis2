"""Per-run event emitter: assigns ``seq``, persists (except deltas), publishes."""

from __future__ import annotations

from jarvis_core.db import Store
from jarvis_core.engine.bus import EventBus
from jarvis_proto import Run
from jarvis_proto.events import ConversationUpdated, ModelDelta, RunEvent


class RunEmitter:
    def __init__(self, run: Run, store: Store, bus: EventBus) -> None:
        self.run = run
        self._store = store
        self._bus = bus

    async def emit(self, event: RunEvent) -> None:
        event.run_id = self.run.id
        event.conversation_id = self.run.conversation_id
        if isinstance(event, ModelDelta):
            event.seq = self.run.last_seq
        else:
            self.run.last_seq += 1
            event.seq = self.run.last_seq
            await self._store.append_event(event)
        self._bus.publish(event)

    async def announce_activity(self) -> None:
        """Re-broadcast the conversation so every sidebar re-reads its activity dot.

        Run events reach only the clients subscribed to this conversation, and the sidebar
        shows all of them. ``conversation.updated`` goes to everyone, and the conversation
        carries its activity, so publishing one is how a chat starts and stops glowing on a
        phone that is looking at a different chat. The engine already does this when a run is
        created and when it ends; this covers the transitions in between (parking on a
        confirmation, and being let go again).
        """
        conv = await self._store.get_conversation(self.run.conversation_id)
        if conv is not None:
            self._bus.publish(ConversationUpdated(conversation=conv))
