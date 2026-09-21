"""Wire-format translation and streaming parsers for the two real adapters (no network)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis_core.models.base import (
    ModelDoneChunk,
    ModelError,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
    endpoint_semaphore,
    reset_endpoint_semaphores,
)
from jarvis_core.models.ollama import OllamaAdapter, to_ollama_messages
from jarvis_core.models.openai_compat import OpenAICompatAdapter, to_openai_messages
from jarvis_proto import Message, ModelSpec, Provider, RoleName, ToolCall


def test_reasoning_is_never_sent_back():
    msgs = [
        Message.user("q"),
        Message.assistant(
            "a", reasoning="secret thoughts", tool_calls=[ToolCall(id="1", name="t", arguments={"x": 1})]
        ),
        Message.tool("1", "t", "result"),
    ]
    for wire in (to_ollama_messages(msgs), to_openai_messages(msgs)):
        assert "secret thoughts" not in json.dumps(wire)
    o = to_ollama_messages(msgs)
    assert o[1]["tool_calls"][0]["function"]["arguments"] == {"x": 1}
    assert o[2] == {"role": "tool", "content": "result", "tool_name": "t"}
    a = to_openai_messages(msgs)
    assert a[1]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'
    assert a[2]["tool_call_id"] == "1"


async def test_endpoint_semaphore_is_shared_and_follows_the_configured_limit():
    """One semaphore per endpoint, rebuilt only when the configured limit itself changes."""
    reset_endpoint_semaphores()
    chat = ModelSpec(provider=Provider.VLLM, base_url="http://gpu/v1", model="a")
    triage = ModelSpec(provider=Provider.VLLM, base_url="http://gpu/v1", model="b")
    other = ModelSpec(provider=Provider.VLLM, base_url="http://other/v1", model="a")

    sem = endpoint_semaphore(chat, 2)
    # Two roles on the same box share one limit; a different box gets its own.
    assert endpoint_semaphore(triage, 2) is sem
    assert endpoint_semaphore(other, 2) is not sem
    # Sharing survives a lookup while permits are held (the old check compared free permits, so
    # an in-flight call made the next role build a second semaphore and the box saw 2x traffic).
    async with sem:
        assert endpoint_semaphore(triage, 2) is sem

    # Lowering the setting tightens the endpoint...
    tighter = endpoint_semaphore(chat, 1)
    assert tighter is not sem and tighter._value == 1
    # ...and raising it again takes effect. It used to be ignored for the life of the process.
    wider = endpoint_semaphore(chat, 4)
    assert wider is not tighter and wider._value == 4
    reset_endpoint_semaphores()


def _mock_transport(lines: list[str], *, status: int = 200) -> httpx.MockTransport:
    body = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode())

    return httpx.MockTransport(handler)


async def _collect(adapter, messages):  # type: ignore[no-untyped-def]
    out = []
    async for chunk in adapter.stream(messages, [], cancel=asyncio.Event()):
        out.append(chunk)
    return out


async def test_ollama_stream_parses_thinking_text_tools_usage():
    spec = ModelSpec(provider=Provider.OLLAMA, base_url="http://x", model="m", think=True, num_ctx=8192)
    adapter = OllamaAdapter(spec)
    adapter._client = httpx.AsyncClient(
        transport=_mock_transport(
            [
                json.dumps({"message": {"role": "assistant", "thinking": "let me "}, "done": False}),
                json.dumps({"message": {"role": "assistant", "content": "Hi"}, "done": False}),
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [{"function": {"name": "jarvis.time", "arguments": {"timezone": "UTC"}}}],
                        },
                        "done": False,
                    }
                ),
                json.dumps(
                    {
                        "message": {"role": "assistant", "content": ""},
                        "done": True,
                        "done_reason": "stop",
                        "prompt_eval_count": 12,
                        "eval_count": 7,
                    }
                ),
            ]
        )
    )
    chunks = await _collect(adapter, [Message.user("hi")])
    assert isinstance(chunks[0], ModelReasoningChunk) and chunks[0].text == "let me "
    assert isinstance(chunks[1], ModelTextChunk) and chunks[1].text == "Hi"
    assert isinstance(chunks[2], ModelToolCallsChunk) and chunks[2].calls[0].name == "jarvis.time"
    done = chunks[-1]
    assert isinstance(done, ModelDoneChunk) and done.usage.prompt_tokens == 12 and done.usage.completion_tokens == 7
    payload = adapter._payload([Message.user("hi")], [])
    assert payload["think"] is True and payload["options"]["num_ctx"] == 8192


async def test_ollama_4xx_is_not_retried_and_thinking_error_is_explained():
    spec = ModelSpec(provider=Provider.OLLAMA, base_url="http://x", model="m", think=True)
    adapter = OllamaAdapter(spec)
    adapter._client = httpx.AsyncClient(
        transport=_mock_transport([json.dumps({"error": "m does not support thinking"})], status=400)
    )
    with pytest.raises(ModelError, match="does not support thinking"):
        await _collect(adapter, [Message.user("hi")])


async def test_openai_compat_accumulates_tool_call_deltas_and_reasoning():
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=False)
    adapter = OpenAICompatAdapter(spec)

    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj)

    adapter._client = httpx.AsyncClient(
        transport=_mock_transport(
            [
                sse({"choices": [{"delta": {"reasoning": "think"}, "finish_reason": None}]}),
                sse({"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}),
                sse({"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}),
                sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "id": "call_1", "function": {"name": "jarvis.", "arguments": ""}}
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"name": "time", "arguments": '{"timezone"'}}
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "UTC"}'}}]},
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 30,
                            "completion_tokens": 9,
                            "prompt_tokens_details": {"cached_tokens": 24},
                        },
                    }
                ),
                "data: [DONE]",
            ]
        )
    )
    chunks = await _collect(adapter, [Message.user("hi")])
    kinds = [type(c).__name__ for c in chunks]
    assert kinds[0] == "ModelReasoningChunk"
    text = "".join(c.text for c in chunks if isinstance(c, ModelTextChunk))
    assert text == "Hello"
    calls = next(c for c in chunks if isinstance(c, ModelToolCallsChunk)).calls
    assert calls[0].id == "call_1" and calls[0].name == "jarvis.time" and calls[0].arguments == {"timezone": "UTC"}
    done = chunks[-1]
    assert isinstance(done, ModelDoneChunk) and done.usage.prompt_tokens == 30 and done.finish_reason == "tool_calls"
    # What the prefix cache served is the server's own number, and it is what TTFT is NOT spent
    # on: 30 prompt tokens of which 24 cached leaves 6 to actually read.
    assert done.usage.cached_tokens == 24 and done.usage.prefilled_tokens == 6
    payload = adapter._payload([Message.user("hi")], [])
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["stream_options"] == {"include_usage": True}


async def test_5xx_before_first_byte_is_retried_then_fails():
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m")
    adapter = OpenAICompatAdapter(spec)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, content=b"busy")

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import jarvis_core.models.http as http_mod

    http_mod._BACKOFF_S = (0.0, 0.0)
    with pytest.raises(ModelError, match="503"):
        await _collect(adapter, [Message.user("hi")])
    assert attempts["n"] == 3


async def test_context_length_400_retries_once_with_max_tokens_that_fit():
    """vLLM's 400 says exactly how much room there is; the adapter asks for that instead of dying."""
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=False, max_tokens=16384)
    adapter = OpenAICompatAdapter(spec)
    seen: list[int | None] = []
    refusal = (
        '{"error":{"message":"This model\'s maximum context length is 65536 tokens. However, you requested '
        "16384 output tokens and your prompt contains at least 49153 input tokens, for a total of at least "
        "65537 tokens. Please reduce the length of the input prompt or the number of requested output tokens. "
        '(parameter=input_tokens, value=49153)","type":"BadRequestError","param":"input_tokens","code":400}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content).get("max_tokens"))
        if len(seen) == 1:
            return httpx.Response(400, content=refusal.encode())
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
            "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 49153, "completion_tokens": 1}}),
            "data: [DONE]",
        ]
        return httpx.Response(200, content="\n".join(lines).encode())

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = await _collect(adapter, [Message.user("hi")])
    # "at least 49153" is context - 16384 + 1, not the prompt's size: the retry halves instead.
    assert seen == [16384, 8192]
    assert any(getattr(e, "text", None) == "ok" for e in events)


async def test_context_length_400_keeps_halving_until_it_fits():
    """A 60K prompt on a 64K server: 16384 and 8192 refused, 4096 fits."""
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=False, max_tokens=16384)
    adapter = OpenAICompatAdapter(spec)
    seen: list[int | None] = []
    prompt = 60000

    def handler(request: httpx.Request) -> httpx.Response:
        want = json.loads(request.content).get("max_tokens")
        seen.append(want)
        if prompt + want > 65536:
            msg = (
                f"This model's maximum context length is 65536 tokens. However, you requested {want} output "
                f"tokens and your prompt contains at least {65536 - want + 1} input tokens, for a total of at "
                "least 65537 tokens."
            )
            return httpx.Response(400, content=json.dumps({"error": {"message": msg, "code": 400}}).encode())
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
            "data: [DONE]",
        ]
        return httpx.Response(200, content="\n".join(lines).encode())

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _collect(adapter, [Message.user("hi")])
    assert seen == [16384, 8192, 4096]


async def test_context_length_400_with_no_room_left_fails_once():
    """When even 256 output tokens would not fit, the prompt is the problem — no second attempt."""
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=False, max_tokens=4096)
    adapter = OpenAICompatAdapter(spec)
    calls = {"n": 0}
    refusal = (
        '{"error":{"message":"This model\'s maximum context length is 65536 tokens. However, you requested '
        "4096 output tokens and your prompt contains at least 65400 input tokens, for a total of at least "
        '69496 tokens.","type":"BadRequestError","code":400}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, content=refusal.encode())

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError, match="maximum context length"):
        await _collect(adapter, [Message.user("hi")])
    assert calls["n"] == 1


async def test_probe_reads_the_context_window_and_the_factory_keeps_it_per_lane():
    """vLLM's /v1/models says max_model_len; the factory asks once per endpoint and num_ctx wins."""
    from jarvis_core.models.factory import AdapterFactory
    from jarvis_proto import RunKind, Settings

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": [{"id": "m", "max_model_len": 131072}]})

    s = Settings()
    s.roles[RoleName.CHAT] = ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=True)
    s.roles[RoleName.BACKGROUND] = ModelSpec(
        provider=Provider.VLLM, base_url="http://y/v1", model="m", think=True, num_ctx=4096
    )
    f = AdapterFactory(s)
    f.for_role(RoleName.CHAT)._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await f.context_window(RunKind.CHAT) == 131072
    assert await f.context_window(RunKind.CHAT) == 131072 and calls["n"] == 1, "asked once, then remembered"
    assert await f.context_window(RunKind.TRIAGE) == 4096, "num_ctx is the explicit answer"
    assert calls["n"] == 1, "a lane with num_ctx is never probed for it"
