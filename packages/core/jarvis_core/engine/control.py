"""Per-run control surface shared by the engine and the loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from jarvis_core.engine.emit import RunEmitter


class RunCancelledError(Exception):
    def __init__(self, partial_message_id: str | None = None) -> None:
        super().__init__("run cancelled")
        self.partial_message_id = partial_message_id


@dataclass(slots=True)
class RunControl:
    emitter: RunEmitter
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    confirmations: dict[str, asyncio.Future[tuple[bool, str | None]]] = field(default_factory=dict)
    resumed: bool = False

    def resolve_confirmation(self, call_id: str, approved: bool, note: str | None) -> bool:
        fut = self.confirmations.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result((approved, note))
        return True
