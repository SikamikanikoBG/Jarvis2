"""Scripted model for tests and CI. Deterministic, fast, and able to misbehave on request."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from jarvis_core.models.base import (
    ModelCancelled,
    ModelDoneChunk,
    ModelError,
    ModelEvent,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
    ProbeResult,
)
from jarvis_proto import Message, ModelSpec, ModelUsage, Provider, ToolCall, ToolSpec


@dataclass(slots=True)
class FakeTurn:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    token_delay_s: float = 0.0  # per text token; lets tests cancel mid-stream
    fail_before_first_byte: bool = False  # transient error → the loop should see retry/fail
    hang: bool = False  # never finishes until cancelled
    prompt_tokens: int = 100
    completion_tokens: int = 20
    cached_tokens: int = 0  # what a prefix cache served, for budget/TTFT tests


class FakeAdapter:
    """Pops one ``FakeTurn`` per ``stream()`` call. Records every call for assertions."""

    def __init__(self, turns: list[FakeTurn] | None = None, *, spec: ModelSpec | None = None) -> None:
        self.spec = spec or ModelSpec(provider=Provider.FAKE, base_url="fake://", model="fake")
        self.turns: list[FakeTurn] = list(turns or [])
        self.calls: list[tuple[list[Message], list[ToolSpec]]] = []
        self.on_call: Callable[[int], None] | None = None
        self.default_turn = FakeTurn(text="(fake) no more scripted turns")

    def push(self, *turns: FakeTurn) -> None:
        self.turns.extend(turns)

    async def stream(
        self, messages: list[Message], tools: list[ToolSpec], *, cancel: asyncio.Event
    ) -> AsyncIterator[ModelEvent]:
        self.calls.append((list(messages), list(tools)))
        if self.on_call:
            self.on_call(len(self.calls))
        turn = self.turns.pop(0) if self.turns else self.default_turn
        if turn.fail_before_first_byte:
            raise ModelError("fake: connection refused", retryable=True)
        if turn.hang:
            await cancel.wait()
            raise ModelCancelled
        if turn.reasoning:
            yield ModelReasoningChunk(turn.reasoning)
        for token in _tokens(turn.text):
            if cancel.is_set():
                raise ModelCancelled
            if turn.token_delay_s:
                await asyncio.sleep(turn.token_delay_s)
                if cancel.is_set():
                    raise ModelCancelled
            yield ModelTextChunk(token)
        if turn.tool_calls:
            yield ModelToolCallsChunk(list(turn.tool_calls))
        yield ModelDoneChunk(
            usage=ModelUsage(
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
                cached_tokens=turn.cached_tokens,
                calls=1,
                ttft_ms=1,
                duration_ms=2,
            ),
            finish_reason="tool_calls" if turn.tool_calls else "stop",
        )

    async def probe(self) -> ProbeResult:
        return ProbeResult(True, 0, "fake", ["fake"])


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    words = text.split(" ")
    return [w if i == len(words) - 1 else w + " " for i, w in enumerate(words)]
