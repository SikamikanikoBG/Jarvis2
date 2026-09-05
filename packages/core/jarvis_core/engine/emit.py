"""Per-run event emitter: assigns ``seq``, persists (except deltas), publishes."""

from __future__ import annotations

from jarvis_core.db import Store
from jarvis_core.engine.bus import EventBus
from jarvis_proto import Run
from jarvis_proto.events import ModelDelta, RunEvent


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
