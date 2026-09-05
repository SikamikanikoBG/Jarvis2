"""OpenAI-compatible adapter (vLLM) over ``/v1/chat/completions`` SSE.

Thinking is controlled through ``chat_template_kwargs.enable_thinking`` (Qwen-style
templates) and surfaced by vLLM in the delta field ``reasoning`` (or ``reasoning_content``
on older servers). Tool-call deltas are accumulated by index and parsed once at the end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from jarvis_core.models.base import (
    ModelDoneChunk,
    ModelError,
    ModelEvent,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
    ProbeResult,
    endpoint_semaphore,
)
from jarvis_core.models.http import Stopwatch, make_client, stream_lines
from jarvis_proto import Message, ModelSpec, ModelUsage, Role, ToolCall, ToolSpec, new_id

log = logging.getLogger(__name__)


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role is Role.TOOL:
            out.append({"role": "tool", "content": m.content, "tool_call_id": m.tool_call_id or ""})
            continue
        item: dict[str, Any] = {"role": m.role.value, "content": m.content}
        if m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in m.tool_calls
            ]
            if not m.content:
                item["content"] = None
        out.append(item)
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
        }
        for t in tools
    ]


class OpenAICompatAdapter:
    def __init__(self, spec: ModelSpec, *, concurrency: int = 1, api_key: str | None = None) -> None:
        self.spec = spec
        self._sem = endpoint_semaphore(spec, concurrency)
        self._client = make_client(spec.timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.spec.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.spec.temperature,
            "chat_template_kwargs": {"enable_thinking": self.spec.think},
        }
        if self.spec.max_tokens:
            payload["max_tokens"] = self.spec.max_tokens
        if tools:
            payload["tools"] = to_openai_tools(tools)
        return payload

    async def stream(
        self, messages: list[Message], tools: list[ToolSpec], *, cancel: asyncio.Event
    ) -> AsyncIterator[ModelEvent]:
        payload = self._payload(messages, tools)
        url = self.spec.base_url.rstrip("/") + "/chat/completions"
        async with self._sem:
            watch = Stopwatch()
            usage = ModelUsage(calls=1)
            finish: str | None = None
            pending: dict[int, dict[str, Any]] = {}
            async for line in stream_lines(self._client, url, payload, cancel=cancel, headers=self._headers):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise ModelError(str(chunk["error"]))
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage.prompt_tokens = int(u.get("prompt_tokens") or 0)
                    usage.completion_tokens = int(u.get("completion_tokens") or 0)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if reasoning:
                        watch.mark_first()
                        yield ModelReasoningChunk(reasoning)
                    content = delta.get("content")
                    if content:
                        watch.mark_first()
                        yield ModelTextChunk(content)
                    for tc in delta.get("tool_calls") or []:
                        watch.mark_first()
                        idx = int(tc.get("index", len(pending)))
                        slot = pending.setdefault(idx, {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
            if pending:
                yield ModelToolCallsChunk(_finalize_tool_calls(pending))
            usage.ttft_ms = watch.ttft_ms
            usage.duration_ms = watch.elapsed_ms
            yield ModelDoneChunk(usage=usage, finish_reason=finish)

    async def probe(self) -> ProbeResult:
        t0 = time.perf_counter()
        try:
            resp = await self._client.get(
                self.spec.base_url.rstrip("/") + "/models", headers=self._headers, timeout=5.0
            )
            ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return ProbeResult(False, ms, f"HTTP {resp.status_code}")
            names = [m.get("id", "") for m in resp.json().get("data", [])]
            has = not self.spec.model or self.spec.model in names
            detail = "" if has else f"model {self.spec.model!r} not served"
            return ProbeResult(has, ms, detail, names)
        except Exception as exc:
            return ProbeResult(False, int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}")


def _finalize_tool_calls(pending: dict[int, dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for idx in sorted(pending):
        slot = pending[idx]
        raw = slot["args"]
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            args = {"__raw__": raw}
        calls.append(
            ToolCall(
                id=slot["id"] or new_id("call"),
                name=slot["name"],
                arguments=args if isinstance(args, dict) else {"__raw__": raw},
            )
        )
    return calls
