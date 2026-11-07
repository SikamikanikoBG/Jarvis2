"""Context ledger: every model call, while it runs and after it ends (idea-to-uat/context-usage).

The adapters are the one place every model call passes through - the main loop, the planner,
the classifier, the judge, the journal, compaction - so they report here and nothing upstream
needs plumbing. While a call is in flight it sits in ``live`` (the Context screen's "now" bar
reads that); when it ends it becomes one row through ``sink`` (features/context_usage.py writes
it to ``model_calls``).

Who a call is for is read from the context the engine already sets per run task: the run kind,
the run and conversation ids, and whether it is a voice turn. Work that runs outside the turn
that started it (journal, compaction, knowledge) says so with ``call_purpose``.

The ledger never fails a model call: a ticket that cannot be written is logged and dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from jarvis_proto import Message, ModelSpec, ModelUsage, ToolSpec, new_id

log = logging.getLogger(__name__)

#: Set by work that runs on its own account even when a run started it (journal, compaction,
#: knowledge): the call is booked to "housekeeping · <purpose>", not to the run.
call_purpose: ContextVar[str | None] = ContextVar("call_purpose", default=None)

#: Set by ContextBuilder when it had to drop older history to fit the window; the next call
#: made in the same task carries it as a "trimmed" overflow and clears it.
pending_trim: ContextVar[int] = ContextVar("pending_trim", default=0)

# Same estimate the context builder starts from before it is calibrated (engine/context.py).
_CHARS_PER_TOKEN = 3.2

# The servers' own words for "this does not fit the context": vLLM ("maximum context length is N
# tokens") and llama.cpp (error type exceed_context_size_error, "exceeds the available context
# size", or mid-generation "Context size has been exceeded"). These are protocol messages being
# parsed, the way _CONTEXT_400 already reads vLLM's 400.
_OVERFLOW_TEXT = re.compile(
    r"maximum context length|exceed_context_size|exceeds? the available context|context size has been exceeded",
    re.I,
)


def is_context_overflow(error: str) -> bool:
    return bool(_OVERFLOW_TEXT.search(error or ""))


def estimate_prompt_tokens(messages: list[Message], tools: list[ToolSpec], chars_per_token: float) -> int:
    chars = sum(len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) for m in messages)
    chars += sum(len(t.name) + len(t.description) + len(json.dumps(t.input_schema)) + 16 for t in tools)
    return int(chars / (chars_per_token or _CHARS_PER_TOKEN))


@dataclass
class CallRecord:
    """One model call. Times are epoch milliseconds."""

    id: str
    endpoint: str
    model: str
    role: str | None
    source: str
    label: str | None
    run_id: str | None
    conversation_id: str | None
    window: int | None
    queued_ms: int
    started_ms: int | None = None
    ended_ms: int | None = None
    prompt_estimate: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    estimated: bool = False  # the server reported no usage: tokens are the char estimate
    streamed_chars: int = 0
    outcome: str = "running"  # running | ok | failed | cancelled
    trimmed_tokens: int = 0
    shrunk: bool = False  # the server refused the output room asked for; retried with less
    rejected: bool = False  # the server refused the prompt as too long for its context
    cut: bool = False  # the answer stopped at the output limit
    error: str | None = None
    _mono0: float = field(default_factory=time.monotonic, repr=False)

    @property
    def state(self) -> str:
        return "waiting" if self.started_ms is None else self.outcome

    @property
    def tokens(self) -> int:
        """Context the call holds: its prompt plus what it has written so far."""
        if self.outcome == "running":
            done = int(self.streamed_chars / _CHARS_PER_TOKEN)
            return max(self.prompt_tokens, self.prompt_estimate) + done
        return self.prompt_tokens + self.completion_tokens

    def as_live(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "endpoint": self.endpoint,
            "model": self.model,
            "role": self.role,
            "source": self.source,
            "label": self.label,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "state": self.state,
            "queued_at": self.queued_ms,
            "started_at": self.started_ms,
            "tokens": self.tokens,
            "window": self.window,
            "trimmed_tokens": self.trimmed_tokens,
        }


Sink = Callable[[CallRecord], Awaitable[None]]
Resolver = Callable[[str], Awaitable[str | None]]  # conversation id -> schedule name


def _now_ms() -> int:
    return int(time.time() * 1000)


def _source_of(kind: Any, voice: bool) -> str:
    if kind is None:
        return "housekeeping"
    value = getattr(kind, "value", str(kind))
    if value == "chat" and voice:
        return "voice"
    return value


class ContextLedger:
    def __init__(
        self,
        *,
        sink: Sink | None = None,
        window_of: Callable[[ModelSpec], Awaitable[int | None]] | None = None,
        schedule_of: Resolver | None = None,
        chars_per_token: Callable[[], float] | None = None,
    ) -> None:
        self.live: dict[str, CallRecord] = {}
        self.sink = sink
        self.window_of = window_of
        self.schedule_of = schedule_of
        self.chars_per_token = chars_per_token
        self._writes: set[asyncio.Task[None]] = set()
        self._labels: dict[str, str | None] = {}

    async def begin(
        self, spec: ModelSpec, role: str | None, messages: list[Message], tools: list[ToolSpec]
    ) -> CallRecord:
        """A call is about to queue for its endpoint. Returns its record; the adapter marks it
        started, finished or failed. Never raises."""
        from jarvis_core.engine.current import current_conversation_id, current_run_id
        from jarvis_core.models.base import VOICE, call_urgency
        from jarvis_core.models.factory import current_run_kind

        purpose = call_purpose.get()
        run_id, conv_id = current_run_id.get(), current_conversation_id.get()
        if purpose:
            source, label = "housekeeping", purpose
        else:
            source = _source_of(current_run_kind.get(), call_urgency.get() == VOICE)
            label = None
            if source == "scheduled" and conv_id:
                label = await self._schedule_name(conv_id)
            elif source == "housekeeping":
                label = role
        window = spec.num_ctx
        if window is None and self.window_of is not None:
            try:
                window = await self.window_of(spec)
            except Exception:
                window = None
        cpt = _CHARS_PER_TOKEN
        if self.chars_per_token is not None:
            try:
                cpt = self.chars_per_token() or _CHARS_PER_TOKEN
            except Exception:
                cpt = _CHARS_PER_TOKEN
        trimmed = pending_trim.get()
        if trimmed:
            pending_trim.set(0)
        rec = CallRecord(
            id=new_id("mc"),
            endpoint=spec.base_url,
            model=spec.model,
            role=role,
            source=source,
            label=label,
            run_id=run_id,
            conversation_id=conv_id,
            window=window,
            queued_ms=_now_ms(),
            prompt_estimate=estimate_prompt_tokens(messages, tools, cpt),
            trimmed_tokens=trimmed,
        )
        self.live[rec.id] = rec
        return rec

    async def _schedule_name(self, conversation_id: str) -> str | None:
        if conversation_id in self._labels:
            return self._labels[conversation_id]
        name = None
        if self.schedule_of is not None:
            try:
                name = await self.schedule_of(conversation_id)
            except Exception:
                name = None
        if len(self._labels) > 2000:
            self._labels.clear()
        self._labels[conversation_id] = name
        return name

    @staticmethod
    def started(rec: CallRecord | None) -> None:
        if rec is not None and rec.started_ms is None:
            rec.started_ms = _now_ms()

    @staticmethod
    def saw(rec: CallRecord | None, text: str) -> None:
        if rec is not None:
            rec.streamed_chars += len(text)

    def finish(
        self, rec: CallRecord | None, usage: ModelUsage, finish_reason: str | None, max_tokens: int | None
    ) -> None:
        if rec is None:
            return
        rec.prompt_tokens = usage.prompt_tokens
        rec.completion_tokens = usage.completion_tokens
        rec.cached_tokens = usage.cached_tokens
        if not usage.prompt_tokens and not usage.completion_tokens:
            rec.estimated = True
            rec.prompt_tokens = rec.prompt_estimate
            rec.completion_tokens = int(rec.streamed_chars / _CHARS_PER_TOKEN)
        rec.cut = finish_reason == "length" or bool(max_tokens and usage.completion_tokens >= max_tokens)
        self._close(rec, "ok")

    def fail(self, rec: CallRecord | None, exc: BaseException) -> None:
        if rec is None:
            return
        from jarvis_core.models.base import ModelCancelled

        if isinstance(exc, ModelCancelled | asyncio.CancelledError | GeneratorExit):
            outcome = "cancelled"
        else:
            outcome = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"[:500]
            if is_context_overflow(str(exc)):
                rec.rejected = True
        if not rec.prompt_tokens:
            rec.estimated = True
            rec.prompt_tokens = rec.prompt_estimate
            rec.completion_tokens = int(rec.streamed_chars / _CHARS_PER_TOKEN)
        self._close(rec, outcome)

    def _close(self, rec: CallRecord, outcome: str) -> None:
        if rec.outcome != "running":
            return  # already closed (a generator closed after it finished)
        rec.outcome = outcome
        rec.ended_ms = _now_ms()
        self.live.pop(rec.id, None)
        if self.sink is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._write(rec))
        except RuntimeError:
            return
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def _write(self, rec: CallRecord) -> None:
        try:
            assert self.sink is not None
            await self.sink(rec)
        except Exception as exc:
            log.warning("context ledger: call %s not recorded: %s", rec.id, exc)

    async def drain(self) -> None:
        """Wait for pending writes (shutdown, tests)."""
        if self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)
