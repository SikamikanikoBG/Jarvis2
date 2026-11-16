"""Model adapter contract.

An adapter turns our ``Message`` list into one streamed model call and yields normalised
chunks. It owns: per-endpoint concurrency, timeouts, retry-before-first-byte, and the
per-role ``think`` switch. It does not own: history, tools, persistence, cancellation policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from jarvis_proto import Message, ModelSpec, ModelUsage, Provider, ToolCall, ToolSpec

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

    def __init__(
        self, message: str, *, retryable: bool = False, status: int | None = None, crowded: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        # 2026-11-16: the server ran out of its shared KV pool while other calls were in flight
        # beside this one, after part of the answer had already streamed. Not this call's fault:
        # the caller may make it again, once, with `call_alone` set.
        self.crowded = crowded


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
#: Set for the one retry of a call that lost the endpoint's shared KV pool to the calls beside it
#: (ModelError.crowded): that call waits until it can run on the endpoint by itself.
call_alone: ContextVar[bool] = ContextVar("call_alone", default=False)
#: The output room a call is assumed to take in a shared KV pool when its spec sets no max_tokens:
#: the same default the run loop budgets its window with (engine/loop.py _DEFAULT_MAX_TOKENS).
DEFAULT_OUTPUT_RESERVE = 16_384


@dataclass(slots=True, eq=False)
class GateHold:
    """One call holding the gate: the KV tokens it was admitted with, whether it holds the
    endpoint alone, and whether any other call was in flight beside it at some point."""

    tokens: int = 0
    alone: bool = False
    crowded: bool = False


class EndpointGate:
    """How many model calls may be in flight on one endpoint (GPU box) at once, most urgent first.

    Scheduled runs share the chat lane with the chat, two calls at a time, and a call's turn used
    to queue behind whatever they had in flight - a long scheduled step is tens of seconds of
    silence on the line. Now a waiting call goes ahead of every less urgent one, and a VOICE call
    may take one slot more than the limit: the engine batches one more request far better than
    Arsen waits for a scheduled step to finish.

    2026-11-16: going ahead of the queue was not enough. At limit 1 a typed chat call still waited
    for the call in flight, and a task's call runs 4-6 minutes (chat waited up to 323 s). So calls
    Arsen waits for (VOICE, TYPED) may also use ``reserved`` slots of their own that background
    calls never take: background room = limit, typed = limit + reserved, voice = one more again.
    Within one urgency the order is strictly first come, first served (a ticket per waiter).

    2026-11-16, later the same day: a free slot is not free room. llama.cpp with ``-kvu`` keeps ONE
    KV pool for all its slots (vader: 98,304 tokens for 4 slots), and a chat call (49k prompt) let
    in by the reserve beside a scheduled call (65k) asked it for 114k: the server answered "Context
    size has been exceeded" to both and both runs failed. So when the endpoint has a shared
    ``pool`` (learned from the server, openai_compat.py) a call also needs its tokens - estimated
    prompt plus output room - to fit beside the tokens of the calls in flight. A call alone is
    always admitted (its own window already bounds it). The order does not change: the first
    waiter that does not fit holds the ones behind it, so a big call is not starved by small ones
    and interactive still goes first. ``pool`` None = no shared pool: vLLM manages its own KV and
    queues what does not fit; Ollama has one num_ctx per loaded model.

    Background calls are not protected from starvation: while interactive calls keep every slot
    of the limit busy, background waits. That is the intent - a person is waiting on one and not
    on the other - and in practice chat is bursty, so background gets the gaps.
    """

    def __init__(self, limit: int, reserved: int = 0, pool: int | None = None) -> None:
        self.limit = limit
        self.reserved = reserved
        self.pool = pool
        self._holds: list[GateHold] = []
        self._next_ticket = 0
        self._waiting: list[list[int]] = [[], [], []]  # per urgency: tickets in arrival order
        self._cond = asyncio.Condition()

    @property
    def busy(self) -> int:
        return len(self._holds)

    @property
    def tokens_in_flight(self) -> int:
        return sum(h.tokens for h in self._holds)

    def room(self, urgency: int) -> int:
        """How many calls may be in flight when a call of this urgency starts."""
        if urgency >= BACKGROUND:
            return self.limit
        return self.limit + self.reserved + (1 if urgency == VOICE else 0)

    def _fits(self, tokens: int, alone: bool) -> bool:
        if not self._holds:
            return True
        if alone or any(h.alone for h in self._holds):
            return False
        return self.pool is None or self.tokens_in_flight + tokens <= self.pool

    def _may_go(self, urgency: int, ticket: int, tokens: int, alone: bool) -> bool:
        return (
            self.busy < self.room(urgency)
            and not any(self._waiting[:urgency])
            and self._waiting[urgency][0] == ticket
            and self._fits(tokens, alone)
        )

    async def acquire(self, urgency: int | None = None, *, tokens: int = 0, alone: bool = False) -> GateHold:
        level = call_urgency.get() if urgency is None else urgency
        hold = GateHold(tokens=tokens, alone=alone)
        async with self._cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiting[level].append(ticket)
            try:
                await self._cond.wait_for(lambda: self._may_go(level, ticket, tokens, alone))
            finally:
                self._waiting[level].remove(ticket)
                # A waiter that gave up (or went) may have been what held the others back.
                self._cond.notify_all()
            if self._holds:
                hold.crowded = True
                for other in self._holds:
                    other.crowded = True
            self._holds.append(hold)
        return hold

    async def release(self, hold: GateHold | None = None) -> None:
        async with self._cond:
            if hold is None and self._holds:
                hold = self._holds[-1]
            if hold is not None and hold in self._holds:
                self._holds.remove(hold)
            self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def admit(self, urgency: int, *, tokens: int = 0, alone: bool = False) -> AsyncGenerator[GateHold, None]:
        """Hold the gate for one call: ``tokens`` of the shared pool, or the whole endpoint."""
        hold = await self.acquire(urgency, tokens=tokens, alone=alone)
        try:
            yield hold
        finally:
            await self.release(hold)

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.release()


_endpoint_semaphores: dict[str, tuple[tuple[int, int], EndpointGate]] = {}


def endpoint_semaphore(spec: ModelSpec, limit: int, reserved: int = 0) -> EndpointGate:
    """One semaphore per endpoint (GPU box), shared by every role that points at it.

    Keyed by the limit that was CONFIGURED, not by how many permits are free. The check used to
    be ``sem._value > limit`` — the remaining permits — which got it wrong in both directions:
    an idle semaphore was rebuilt on every lookup (so roles stopped sharing one), and once the
    permits had been spent a *raised* ``max_concurrent_runs_per_endpoint`` was ignored, leaving
    the endpoint pinned to the old, narrower limit until the process restarted.
    """
    key = spec.endpoint_key
    current = _endpoint_semaphores.get(key)
    if current is None or current[0] != (limit, reserved):
        current = ((limit, reserved), EndpointGate(limit, reserved))
        _endpoint_semaphores[key] = current
    return current[1]


class SlotPool:
    """Which llama.cpp KV slot (``id_slot``) a call is sent to, so work does not evict each other.

    llama.cpp keeps one cached prompt per slot and, left to choose, gives a request any idle slot
    - a task's 50k-token prompt landed in the slot that held the chat's, and the next chat call
    re-prefilled 65k tokens from nothing (2026-11-15: 71-107 s, cached_tokens 0). Here the first
    ``interactive`` slots serve calls Arsen waits for and the rest serve background work; inside
    its class a call goes back to the slot it used last (same conversation and role - the main
    loop and the planner have different prompts and must not take turns in one slot), else to
    the free slot used longest ago. When every slot of its class is busy it shares one: llama.cpp
    queues it behind that slot, which is still better than evicting the other class.
    """

    def __init__(self, total: int, interactive: int) -> None:
        self.total = total
        self.interactive = interactive
        split = max(1, min(interactive, total - 1))
        self._classes = (list(range(split)), list(range(split, total)))
        self._busy = [0] * total
        self._key: list[object] = [None] * total
        self._used = [0] * total
        self._tick = 0

    def acquire(self, interactive: bool, key: object) -> int:
        ids = self._classes[0 if interactive else 1]
        free = [i for i in ids if self._busy[i] == 0]
        pool = free or ids
        mine = [i for i in pool if self._key[i] == key]
        slot = mine[0] if mine else min(pool, key=lambda i: (self._busy[i], self._used[i]))
        self._tick += 1
        self._busy[slot] += 1
        self._key[slot] = key
        self._used[slot] = self._tick
        return slot

    def release(self, slot: int) -> None:
        self._busy[slot] = max(0, self._busy[slot] - 1)


_slot_pools: dict[str, SlotPool] = {}


def slot_pool(spec: ModelSpec, total: int) -> SlotPool:
    """One slot pool per endpoint, rebuilt when the slot count or the interactive share changes."""
    pool = _slot_pools.get(spec.endpoint_key)
    if pool is None or (pool.total, pool.interactive) != (total, spec.interactive_slots):
        pool = SlotPool(total, spec.interactive_slots)
        _slot_pools[spec.endpoint_key] = pool
    return pool


@dataclass(slots=True)
class ServerFacts:
    """What an OpenAI-compatible endpoint turned out to be when asked (openai_compat.py)."""

    kind: Provider
    slots: int | None = None  # llama.cpp's total_slots
    # The KV pool all its slots share, in tokens (llama.cpp -kvu: every slot reports the whole
    # pool as its n_ctx). None = no shared pool, or not known: calls are admitted by count only.
    kv_pool: int | None = None
    retry_at: float = 0.0  # when a look that failed is taken again (monotonic); 0 = settled
    priority: bool = True  # False once a vLLM server refused a non-zero priority


server_facts: dict[str, ServerFacts] = {}


def reset_endpoint_semaphores() -> None:
    _endpoint_semaphores.clear()
    _slot_pools.clear()
    server_facts.clear()


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
