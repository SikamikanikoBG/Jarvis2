"""Ollama adapter over the native ``/api/chat`` NDJSON API.

Native rather than the OpenAI-compat endpoint because three things V1 learned the hard way
live only here: ``options.num_ctx`` (VRAM), ``think`` (per-role), ``keep_alive``.
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


def to_ollama_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role is Role.TOOL:
            out.append({"role": "tool", "content": m.content, "tool_name": m.name or ""})
            continue
        item: dict[str, Any] = {"role": m.role.value, "content": m.content}
        # Ollama takes images as bare base64 on the message, not as content parts.
        images = [a.data_url.split(",", 1)[-1] for a in m.attachments if a.data_url]
        if images:
            item["images"] = images
        if m.tool_calls:
            item["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
        # Reasoning is deliberately not re-sent.
        out.append(item)
    return out


def to_ollama_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
        }
        for t in tools
    ]


class OllamaAdapter:
    def __init__(self, spec: ModelSpec, *, concurrency: int = 1) -> None:
        self.spec = spec
        self._sem = endpoint_semaphore(spec, concurrency)
        self._client = make_client(spec.timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": self.spec.temperature}
        if self.spec.num_ctx:
            options["num_ctx"] = self.spec.num_ctx
        if self.spec.max_tokens:
            options["num_predict"] = self.spec.max_tokens
        # Ollama takes a bool, or a level string for models that support levels (gpt-oss).
        think: bool | str = self.spec.think
        if self.spec.think and self.spec.think_level:
            think = self.spec.think_level
        payload: dict[str, Any] = {
            "model": self.spec.model,
            "messages": to_ollama_messages(messages),
            "stream": True,
            "options": options,
            "think": think,
        }
        if tools:
            payload["tools"] = to_ollama_tools(tools)
        if self.spec.keep_alive:
            payload["keep_alive"] = self.spec.keep_alive
        return payload

    async def stream(
        self, messages: list[Message], tools: list[ToolSpec], *, cancel: asyncio.Event
    ) -> AsyncIterator[ModelEvent]:
        payload = self._payload(messages, tools)
        url = self.spec.base_url.rstrip("/") + "/api/chat"
        async with self._sem:
            watch = Stopwatch()
            usage = ModelUsage(calls=1)
            finish: str | None = None
            try:
                async for line in stream_lines(self._client, url, payload, cancel=cancel):
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise ModelError(str(chunk["error"]))
                    msg = chunk.get("message") or {}
                    thinking = msg.get("thinking")
                    if thinking:
                        watch.mark_first()
                        yield ModelReasoningChunk(thinking)
                    content = msg.get("content")
                    if content:
                        watch.mark_first()
                        yield ModelTextChunk(content)
                    raw_calls = msg.get("tool_calls")
                    if raw_calls:
                        watch.mark_first()
                        yield ModelToolCallsChunk(_parse_tool_calls(raw_calls))
                    if chunk.get("done"):
                        finish = chunk.get("done_reason")
                        usage.prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                        usage.completion_tokens = int(chunk.get("eval_count") or 0)
            except ModelError as exc:
                if exc.status == 400 and "think" in str(exc).lower() and payload.get("think"):
                    # The model cannot think (or cannot take a level); say so loudly, never degrade silently.
                    what = "thinking levels" if isinstance(payload["think"], str) else "thinking"
                    raise ModelError(
                        f"model {self.spec.model!r} does not support {what}; adjust the role's "
                        f"think/think_level setting ({exc})",
                        status=400,
                    ) from exc
                raise
            usage.ttft_ms = watch.ttft_ms
            usage.duration_ms = watch.elapsed_ms
            yield ModelDoneChunk(usage=usage, finish_reason=finish)

    async def probe(self) -> ProbeResult:
        t0 = time.perf_counter()
        try:
            resp = await self._client.get(self.spec.base_url.rstrip("/") + "/api/tags", timeout=5.0)
            ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return ProbeResult(False, ms, f"HTTP {resp.status_code}")
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            has = any(n == self.spec.model or n.split(":")[0] == self.spec.model for n in names)
            detail = "" if has or not self.spec.model else f"model {self.spec.model!r} not installed"
            return ProbeResult(has or not self.spec.model, ms, detail, names)
        except Exception as exc:
            return ProbeResult(False, int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}")


def _parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw:
        fn = item.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"__raw__": args}
        calls.append(
            ToolCall(
                id=item.get("id") or new_id("call"),
                name=str(fn.get("name") or ""),
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return calls
