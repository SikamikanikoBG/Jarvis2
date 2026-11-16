"""OpenAI-compatible adapter (vLLM, llama.cpp) over ``/v1/chat/completions`` SSE.

Thinking is controlled through ``chat_template_kwargs.enable_thinking`` (Qwen-style
templates) and surfaced by vLLM in the delta field ``reasoning`` (or ``reasoning_content``
on older servers). Tool-call deltas are accumulated by index and parsed once at the end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

from jarvis_core.models.base import (
    BACKGROUND,
    ModelDoneChunk,
    ModelError,
    ModelEvent,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
    ProbeResult,
    ServerFacts,
    call_urgency,
    endpoint_semaphore,
    server_facts,
    slot_pool,
)
from jarvis_core.models.http import Stopwatch, make_client, stream_lines
from jarvis_core.models.ledger import CallRecord, ContextLedger
from jarvis_proto import AttachmentKind, Message, ModelSpec, ModelUsage, Provider, Role, ToolCall, ToolSpec, new_id

log = logging.getLogger(__name__)


def _cap_media(messages: list[Message], max_media: int) -> list[Message]:
    """Keep pixels for the newest ``max_media`` pictures/clips in the whole prompt; the rest become a
    line in the text. vLLM refuses the entire request past its per-prompt limit ("At most 6 image(s)
    may be provided in one prompt", 2026-09-28 - browser screenshots of one LinkedIn run)."""
    keep = max_media
    capped: list[Message] = []
    for m in reversed(messages):
        media = [a for a in m.attachments if a.data_url and a.kind in (AttachmentKind.IMAGE, AttachmentKind.VIDEO)]
        if not media:
            capped.append(m)
            continue
        kept_ids = {a.id for a in media[-keep:]} if keep > 0 else set()
        keep -= len(kept_ids)
        dropped = [a for a in media if a.id not in kept_ids]
        if not dropped:
            capped.append(m)
            continue
        atts = [a.model_copy(update={"data_url": None}) if a in dropped else a for a in m.attachments]
        note = f"[{len(dropped)} earlier picture(s) not shown again - the model takes at most {max_media} per request]"
        capped.append(
            m.model_copy(update={"attachments": atts, "content": f"{m.content}\n{note}" if m.content else note})
        )
    return list(reversed(capped))


def to_openai_messages(messages: list[Message], max_media: int | None = None) -> list[dict[str, Any]]:
    if max_media is not None:
        messages = _cap_media(messages, max_media)
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


# The looks at what a server is that are under way, one per endpoint (strong references).
_looking: dict[str, asyncio.Task[ServerFacts]] = {}
# How long a failed look at what the server is waits before the next one, in seconds.
_DETECT_RETRY_S = 60.0


def _server_root(base_url: str) -> str:
    """llama-server answers /props at its root, beside /v1."""
    root = base_url.rstrip("/")
    return root.removesuffix("/v1")


def _nothing() -> None:
    pass


def _priority_refused(error: str) -> bool:
    """vLLM's answer to a non-zero `priority` when it schedules first come, first served."""
    return "priority" in error.lower() and "schedul" in error.lower()


class OpenAICompatAdapter:
    def __init__(
        self,
        spec: ModelSpec,
        *,
        concurrency: int = 1,
        reserved: int = 0,
        api_key: str | None = None,
        role: str | None = None,
        ledger: ContextLedger | None = None,
    ) -> None:
        self.spec = spec
        self.role = role
        self._ledger = ledger
        self._sem = endpoint_semaphore(spec, concurrency, reserved)
        self._client = make_client(spec.timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def server(self) -> ServerFacts:
        """What the endpoint really is - llama.cpp or vLLM - and, for llama.cpp, how many slots it
        has. Asked once per endpoint and kept; a look that failed is taken again after a minute.

        The provider in settings says how to talk; it cannot always say who answers: the live
        endpoint was set up as "vllm" and is llama.cpp (2026-11-16). /v1/models names its owner
        (`owned_by: llamacpp`) and llama-server's /props has `total_slots`. An explicit "llamacpp"
        skips the first question; an explicit "vllm" on a vLLM server costs one GET, once."""
        key = self.spec.endpoint_key
        facts = server_facts.get(key)
        if facts is not None and (facts.retry_at == 0.0 or time.monotonic() < facts.retry_at):
            return facts
        kind = self.spec.provider
        try:
            if kind is not Provider.LLAMACPP:
                resp = await self._client.get(
                    self.spec.base_url.rstrip("/") + "/models", headers=self._headers, timeout=5.0
                )
                owners = {str(m.get("owned_by", "")).lower() for m in resp.json().get("data", [])}
                if "llamacpp" in owners:
                    kind = Provider.LLAMACPP
            slots: int | None = None
            if kind is Provider.LLAMACPP:
                resp = await self._client.get(
                    _server_root(self.spec.base_url) + "/props", headers=self._headers, timeout=5.0
                )
                total = resp.json().get("total_slots")
                slots = int(total) if total else None
            facts = ServerFacts(kind=kind, slots=slots)
        except Exception as exc:  # not knowing costs only the speed levers, never the call
            log.info("model endpoint %s: could not tell what server it is: %s", key, exc)
            facts = ServerFacts(kind=kind, retry_at=time.monotonic() + _DETECT_RETRY_S)
        server_facts[key] = facts
        return facts

    def _known(self) -> ServerFacts:
        """What is known about the server right now, without waiting for anyone to answer. The
        first call to an endpoint starts the look in the background and goes out as the settings
        say (a call holding the gate must not also wait on a GET); every call after that has the
        answer."""
        key = self.spec.endpoint_key
        facts = server_facts.get(key)
        if facts is not None and (facts.retry_at == 0.0 or time.monotonic() < facts.retry_at):
            return facts
        if key not in _looking:
            look = asyncio.create_task(self.server(), name=f"server-kind {key}")
            _looking[key] = look
            look.add_done_callback(lambda _: _looking.pop(key, None))
        return facts or ServerFacts(kind=self.spec.provider)

    async def _native(self, payload: dict[str, Any], urgency: int) -> tuple[dict[str, Any], Callable[[], None]]:
        """The request with the server's own speed levers for a call of this urgency, and the
        llama.cpp slot it took (given back when the call ends).

        llama.cpp: `cache_prompt` (its prefix cache; the default in recent builds, said anyway)
        and, with slot pinning, `id_slot` - interactive calls in their slots, background in the
        rest, so a task never evicts the chat's cached prefix. Verified on the live server
        2026-11-16: a request with id_slot 3 ran in slot 3, the same prompt again there came back
        1511 of 1515 tokens cached, and the same prompt in another slot 0 cached.
        vLLM: `priority` only when the server schedules by it (lower = sooner; urgency is already
        0 voice, 1 typed, 2 background); its automatic prefix caching needs nothing in the request.
        Ollama has its own adapter (ollama.py)."""
        facts = self._known()
        if facts.kind is Provider.LLAMACPP:
            if self.spec.slot_pinning and facts.slots and facts.slots >= 2:
                pool = slot_pool(self.spec, facts.slots)
                slot = pool.acquire(urgency < BACKGROUND, self._affinity())
                return {**payload, "cache_prompt": True, "id_slot": slot}, lambda: pool.release(slot)
            return {**payload, "cache_prompt": True}, _nothing
        if facts.kind is Provider.VLLM and self.spec.priority_scheduling and facts.priority:
            return {**payload, "priority": urgency}, _nothing
        return payload, _nothing

    def _affinity(self) -> tuple[str | None, str | None]:
        """Calls that should share a slot: the same conversation and the same role (the main loop
        and the planner send different prompts; taking turns in one slot evicts both)."""
        from jarvis_core.engine.current import current_conversation_id

        return current_conversation_id.get(), self.role

    def _payload(self, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.spec.model,
            "messages": to_openai_messages(messages, self.spec.max_media),
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
        ledger = self._ledger
        rec = await ledger.begin(self.spec, self.role, messages, tools) if ledger else None
        try:
            # aclosing: when the caller stops early, the inner stream must release the endpoint's
            # slot now, not whenever the garbage collector finalises it.
            async with contextlib.aclosing(self._stream(url, payload, rec, cancel)) as events:
                async for event in events:
                    yield event
        except BaseException as exc:
            if ledger:
                ledger.fail(rec, exc)
            raise

    async def _stream(
        self, url: str, payload: dict[str, Any], rec: CallRecord | None, cancel: asyncio.Event
    ) -> AsyncGenerator[ModelEvent, None]:
        urgency = call_urgency.get()
        async with self._sem:
            # The slot is chosen once the call holds the gate: only then is "which slots are
            # free" true of the moment the request goes out.
            payload, give_back = await self._native(payload, urgency)
            try:
                async with contextlib.aclosing(self._events(url, payload, rec, cancel)) as events:
                    async for event in events:
                        yield event
            finally:
                give_back()

    async def _events(
        self, url: str, payload: dict[str, Any], rec: CallRecord | None, cancel: asyncio.Event
    ) -> AsyncGenerator[ModelEvent, None]:
        ContextLedger.started(rec)
        watch = Stopwatch()
        usage = ModelUsage(calls=1)
        finish: str | None = None
        pending: dict[int, dict[str, Any]] = {}
        async for line in self._lines(url, payload, cancel=cancel, rec=rec):
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
                    ContextLedger.saw(rec, reasoning)
                    yield ModelReasoningChunk(reasoning)
                content = delta.get("content")
                if content:
                    watch.mark_first()
                    ContextLedger.saw(rec, content)
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
                        ContextLedger.saw(rec, fn["arguments"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if pending:
            yield ModelToolCallsChunk(_finalize_tool_calls(pending))
        usage.ttft_ms = watch.ttft_ms
        usage.duration_ms = watch.elapsed_ms
        if self._ledger:
            self._ledger.finish(rec, usage, finish, self.spec.max_tokens)
        yield ModelDoneChunk(usage=usage, finish_reason=finish)

    async def _lines(
        self, url: str, payload: dict[str, Any], *, cancel: asyncio.Event, rec: CallRecord | None = None
    ) -> AsyncIterator[str]:
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
                if exc.status == 400 and "priority" in payload and _priority_refused(str(exc)):
                    # The spec said the server schedules by priority; it does not. Go again
                    # without it, and stop sending it to this endpoint.
                    log.warning("model endpoint %s does not schedule by priority; not sending it", url)
                    if (facts := server_facts.get(self.spec.endpoint_key)) is not None:
                        facts.priority = False
                    payload = {k: v for k, v in payload.items() if k != "priority"}
                    continue
                requested = int(payload.get("max_tokens") or 0)
                if exc.status != 400 or requested <= 0:
                    raise
                if (smaller := _smaller_max_tokens(str(exc), requested)) is None:
                    raise
            log.warning(
                "model %s: prompt too long for max_tokens=%d, retrying with %d", self.spec.model, requested, smaller
            )
            if rec is not None:
                rec.shrunk = True
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
