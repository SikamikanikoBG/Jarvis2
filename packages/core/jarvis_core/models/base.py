"""Model adapter contract.

An adapter turns our ``Message`` list into one streamed model call and yields normalised
chunks. It owns: per-endpoint concurrency, timeouts, retry-before-first-byte, and the
per-role ``think`` switch. It does not own: history, tools, persistence, cancellation policy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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


_endpoint_semaphores: dict[str, tuple[int, asyncio.Semaphore]] = {}


def endpoint_semaphore(spec: ModelSpec, limit: int) -> asyncio.Semaphore:
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
        current = (limit, asyncio.Semaphore(limit))
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
