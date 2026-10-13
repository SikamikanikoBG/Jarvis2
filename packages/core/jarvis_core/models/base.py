"""Model adapter contract.

An adapter turns our ``Message`` list into one streamed model call and yields normalised
chunks. It owns: per-endpoint concurrency, timeouts, retry-before-first-byte, and the
per-role ``think`` switch. It does not own: history, tools, persistence, cancellation policy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from jarvis_proto import Message, ModelSpec, ModelUsage, ToolCall, ToolSpec

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ModelTextChunk:
    text: str


@dataclass(slots=True)
class ModelReasoningChunk:
    text: str


@dataclass(slots=True)
class ModelToolCallsChunk:
    calls: list[ToolCall]


@dataclass(slots=True)
class ModelDoneChunk:
    usage: ModelUsage = field(default_factory=ModelUsage)
    finish_reason: str | None = None


ModelEvent = ModelTextChunk | ModelReasoningChunk | ModelToolCallsChunk | ModelDoneChunk


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    latency_ms: int
    detail: str = ""
    models: list[str] = field(default_factory=list)
    # The served model's context window in tokens, when the endpoint says (vLLM's
    # /v1/models carries max_model_len). None = unknown; the context ceiling then stays
    # at the configured budgets.
    context_window: int | None = None


class ModelError(RuntimeError):
    """A model call failed for good (after retries, or a non-retryable status)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ModelCancelled(Exception):
    """Raised inside an adapter when the run's cancel event fires mid-stream."""


class ModelAdapter(Protocol):
    spec: ModelSpec

    def stream(
        self, messages: list[Message], tools: list[ToolSpec], *, cancel: asyncio.Event
    ) -> AsyncIterator[ModelEvent]: ...

    async def probe(self) -> ProbeResult: ...


#: How urgent the model calls of the task in progress are: VOICE (Arsen is on the line, waiting
#: in silence), TYPED (he is looking at the chat) or BACKGROUND (everything else). The engine sets
#: it per run; a call made outside any run is background.
VOICE, TYPED, BACKGROUND = 0, 1, 2
call_urgency: ContextVar[int] = ContextVar("call_urgency", default=BACKGROUND)


class EndpointGate:
    """How many model calls may be in flight on one endpoint (GPU box) at once, most urgent first.

    Scheduled runs share the chat lane with the chat, two calls at a time, and a call's turn used
    to queue behind whatever they had in flight - a long scheduled step is tens of seconds of
    silence on the line. Now a waiting call goes ahead of every less urgent one, and a VOICE call
    may take one slot more than the limit: the engine batches one more request far better than
    Arsen waits for a scheduled step to finish.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._busy = 0
        self._waiting = [0, 0, 0]  # per urgency
        self._cond = asyncio.Condition()

    @property
    def busy(self) -> int:
        return self._busy

    def _may_go(self, urgency: int) -> bool:
        room = self.limit + (1 if urgency == VOICE else 0)
        return self._busy < room and not any(self._waiting[:urgency])

    async def acquire(self, urgency: int | None = None) -> None:
        level = call_urgency.get() if urgency is None else urgency
        async with self._cond:
            self._waiting[level] += 1
            try:
                await self._cond.wait_for(lambda: self._may_go(level))
            finally:
                self._waiting[level] -= 1
                # A waiter that gave up may have been what held the less urgent ones back.
                self._cond.notify_all()
            self._busy += 1

    async def release(self) -> None:
        async with self._cond:
            self._busy -= 1
            self._cond.notify_all()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.release()


_endpoint_semaphores: dict[str, tuple[int, EndpointGate]] = {}


def endpoint_semaphore(spec: ModelSpec, limit: int) -> EndpointGate:
    """One semaphore per endpoint (GPU box), shared by every role that points at it.

    Keyed by the limit that was CONFIGURED, not by how many permits are free. The check used to
    be ``sem._value > limit`` — the remaining permits — which got it wrong in both directions:
    an idle semaphore was rebuilt on every lookup (so roles stopped sharing one), and once the
    permits had been spent a *raised* ``max_concurrent_runs_per_endpoint`` was ignored, leaving
    the endpoint pinned to the old, narrower limit until the process restarted.
    """
    key = spec.endpoint_key
    current = _endpoint_semaphores.get(key)
    if current is None or current[0] != limit:
        current = (limit, EndpointGate(limit))
        _endpoint_semaphores[key] = current
    return current[1]


def reset_endpoint_semaphores() -> None:
    _endpoint_semaphores.clear()


async def wait_cancellable(coro_event: asyncio.Event, cancel: asyncio.Event) -> None:
    """Helper for adapters that need to await something while honouring cancel."""
    waiter = asyncio.create_task(coro_event.wait())
    canceller = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait({waiter, canceller}, return_when=asyncio.FIRST_COMPLETED)
        if canceller in done:
            raise ModelCancelled
    finally:
        waiter.cancel()
        canceller.cancel()
