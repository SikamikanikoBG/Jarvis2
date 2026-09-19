"""OpenAI-compatible adapter (vLLM) over ``/v1/chat/completions`` SSE.

Thinking is controlled through ``chat_template_kwargs.enable_thinking`` (Qwen-style
templates) and surfaced by vLLM in the delta field ``reasoning`` (or ``reasoning_content``
on older servers). Tool-call deltas are accumulated by index and parsed once at the end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
from jarvis_proto import AttachmentKind, Message, ModelSpec, ModelUsage, Role, ToolCall, ToolSpec, new_id

log = logging.getLogger(__name__)


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role is Role.TOOL:
            out.append({"role": "tool", "content": m.content, "tool_call_id": m.tool_call_id or ""})
            # A tool that came back with a picture (browser.screenshot): the tool message
            # carries the text, and the picture follows as a user turn — the one place the
            # OpenAI shape lets an image in. Hydrated only while the request is built.
            pics = [a for a in m.attachments if a.data_url and a.kind is AttachmentKind.IMAGE]
            if pics:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"[what {m.name or 'the tool'} captured — the image below is its result, not a message from Arsen]",
                            },
                            *({"type": "image_url", "image_url": {"url": a.data_url}} for a in pics),
                        ],
                    }
                )
            continue
        shown = [a for a in m.attachments if a.data_url]
        if shown:
            # OpenAI-style multimodal content: the text first, then what there is to look at.
            # A clip goes in a `video_url` part — the shape vLLM takes for a video, verified
            # against the qwen3.8 endpoint on 2026-09-13 (an mp4 data: URI, answered correctly).
            item = {
                "role": m.role.value,
                "content": [
                    *([{"type": "text", "text": m.content}] if m.content else []),
                    *(
                        {"type": "video_url", "video_url": {"url": a.data_url}}
                        if a.kind is AttachmentKind.VIDEO
                        else {"type": "image_url", "image_url": {"url": a.data_url}}
                        for a in shown
                    ),
                ],
            }
            out.append(item)
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


# vLLM refuses a request whose prompt plus `max_tokens` would not fit the model's context —
# it does not trim, it answers 400. A fixed max_tokens therefore turns a long-but-legal prompt
# into a dead run (2026-09-15: ~49K prompt tokens after two news searches + 16,384 requested on
# a 65,536 server). The 400 quotes three numbers, but "your prompt contains at least N input
# tokens" is NOT the prompt's size — it is context − max_tokens + 1, the smallest prompt that
# would overshoot — so the exact fit cannot be read off it. What it does give is a floor on
# the prompt, and the retry halves max_tokens while staying under context − floor.
_CONTEXT_400 = re.compile(
    r"maximum context length is (\d+) tokens.*?requested (\d+) output tokens.*?at least (\d+) input tokens",
    re.S,
)
# Below this many output tokens a retry is not worth it: the prompt itself is the problem, and
# the run should fail with the server's own message.
_MIN_OUTPUT_TOKENS = 256


def _smaller_max_tokens(error: str, requested: int) -> int | None:
    """The next `max_tokens` to try after a context-length 400, or None when none is worth it."""
    m = _CONTEXT_400.search(error)
    if not m:
        return None
    context, _, prompt_floor = (int(g) for g in m.groups())
    candidate = min(requested // 2, context - prompt_floor - 16)
    return candidate if candidate >= _MIN_OUTPUT_TOKENS else None


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
        if self.spec.think and self.spec.think_level:
            payload["reasoning_effort"] = self.spec.think_level
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
            async for line in self._lines(url, payload, cancel=cancel):
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
                    # vLLM reports what its prefix cache served. It is the only honest measure of
                    # whether the prompt stayed append-only; everything else is inferred from
                    # timings that vary with load.
                    details = u.get("prompt_tokens_details") or {}
                    usage.cached_tokens = int(details.get("cached_tokens") or 0)
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

    async def _lines(self, url: str, payload: dict[str, Any], *, cancel: asyncio.Event) -> AsyncIterator[str]:
        """`stream_lines`, retried with a smaller `max_tokens` while the server refuses the request
        for not fitting its context. A 400 arrives before any byte of the answer, so nothing has
        been yielded yet and a retry cannot duplicate events; each one costs a round trip, not a
        prefill. Halving from 16,384 reaches the 256 floor in six steps."""
        while True:
            try:
                async for line in stream_lines(self._client, url, payload, cancel=cancel, headers=self._headers):
                    yield line
                return
            except ModelError as exc:
                requested = int(payload.get("max_tokens") or 0)
                if exc.status != 400 or requested <= 0:
                    raise
                if (smaller := _smaller_max_tokens(str(exc), requested)) is None:
                    raise
            log.warning(
                "model %s: prompt too long for max_tokens=%d, retrying with %d", self.spec.model, requested, smaller
            )
            payload = {**payload, "max_tokens": smaller}

    async def probe(self) -> ProbeResult:
        t0 = time.perf_counter()
        try:
            resp = await self._client.get(
                self.spec.base_url.rstrip("/") + "/models", headers=self._headers, timeout=5.0
            )
            ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return ProbeResult(False, ms, f"HTTP {resp.status_code}")
            data = resp.json().get("data", [])
            names = [m.get("id", "") for m in data]
            has = not self.spec.model or self.spec.model in names
            detail = "" if has else f"model {self.spec.model!r} not served"
            served = next((m for m in data if m.get("id") == self.spec.model), data[0] if data else {})
            window = served.get("max_model_len")
            return ProbeResult(has, ms, detail, names, context_window=int(window) if window else None)
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
