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
from jarvis_proto import Message, ModelSpec, Provider, ToolCall


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
                sse({"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9}}),
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
