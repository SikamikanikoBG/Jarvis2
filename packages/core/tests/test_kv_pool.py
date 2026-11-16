"""A llama.cpp whose slots share one KV pool (2026-11-16, vader `-c 98304 -np 4 -kvu`): a chat call
(49k prompt) let in by the reserved interactive slot beside a scheduled call (65k) asked the pool for
114k of its 98k, the server answered "Context size has been exceeded" to both and both runs failed.
Calls are now admitted by the tokens they need, and a call that still runs out beside others goes
again alone instead of failing."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis_core.engine.current import current_conversation_id
from jarvis_core.models.base import (
    BACKGROUND,
    TYPED,
    EndpointGate,
    ModelError,
    ModelTextChunk,
    call_alone,
    call_urgency,
    endpoint_semaphore,
    reset_endpoint_semaphores,
    server_facts,
)
from jarvis_core.models.fake import FakeTurn
from jarvis_core.models.ollama import OllamaAdapter
from jarvis_core.models.openai_compat import OpenAICompatAdapter, shared_kv_pool
from jarvis_proto import Message, ModelSpec, Provider

# What vader's llama-server answered on 2026-11-16 (GET /props, /slots, /v1/models), trimmed to the
# fields that matter and the shape they come in.
_PARAMS = {"temperature": 0.7, "max_tokens": 16384, "n_predict": 16384, "stream": True}
REAL_PROPS = {
    "default_generation_settings": {"params": {"max_tokens": -1, "n_predict": -1}, "n_ctx": 98304},
    "total_slots": 4,
    "model_alias": "qwen3.8-flash-next",
    "model_ftype": "Q8_0",
    "endpoint_slots": True,
    "endpoint_props": False,
    "endpoint_metrics": True,
    "build_info": "b0-unknown",
    "is_sleeping": False,
}
REAL_SLOTS = [
    {
        "id": i,
        "seq_id": i,
        "n_ctx": 98304,
        "speculative": False,
        "is_processing": False,
        "id_task": 894311491 + i,
        "n_prompt_tokens": 0,
        "n_prompt_tokens_processed": 0,
        "n_prompt_tokens_cache": 0,
        "prefill_waiting": False,
        "prefill_wait_ms": 0.0,
        "params": _PARAMS,
        "next_token": [{"has_next_token": False, "has_new_line": False, "n_remain": -1, "n_decoded": 0}],
    }
    for i in range(4)
]
REAL_MODELS = {
    "models": [{"name": "qwen3.8-flash-next", "model": "qwen3.8-flash-next", "type": "model"}],
    "object": "list",
    "data": [
        {
            "id": "qwen3.8-flash-next",
            "aliases": ["qwen3.8-flash-next"],
            "object": "model",
            "owned_by": "llamacpp",
            "meta": {"vocab_type": True, "n_vocab": 248320, "n_ctx": 98304, "n_ctx_train": 262144},
        }
    ],
}
# The message the fork sends mid-stream when the pool runs out (model_calls 11:36:32 and 11:37:37).
OVERFLOW = {"code": 500, "message": "Context size has been exceeded.", "type": "server_error"}
MODEL = "qwen3.8-flash-next"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    reset_endpoint_semaphores()


# --- learning the pool from the server ----------------------------------------------------------


def test_the_real_unified_server_has_one_pool_of_its_whole_context():
    assert shared_kv_pool(REAL_PROPS, REAL_SLOTS, 98304) == 98304
    # /slots off (501) or /v1/models without meta: still the pool, from what is left.
    assert shared_kv_pool(REAL_PROPS, None, 98304) == 98304
    assert shared_kv_pool(REAL_PROPS, REAL_SLOTS, None) == 98304


def test_slots_with_their_own_share_or_a_single_slot_have_no_shared_pool():
    split_props = {**REAL_PROPS, "default_generation_settings": {"n_ctx": 24576}}
    split_slots = [{**s, "n_ctx": 24576} for s in REAL_SLOTS]
    assert shared_kv_pool(split_props, split_slots, 98304) is None, "-c 98304 -np 4 without -kvu"
    assert shared_kv_pool({**REAL_PROPS, "total_slots": 1}, REAL_SLOTS[:1], 98304) is None
    assert shared_kv_pool({"total_slots": 4}, None, None) is None, "nothing said about n_ctx"


def _llama(*, slots_ok: bool = True, handler=None):
    async def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/v1/models"):
            return httpx.Response(200, json=REAL_MODELS)
        if request.method == "GET" and path.endswith("/props"):
            return httpx.Response(200, json=REAL_PROPS)
        if request.method == "GET" and path.endswith("/slots"):
            return httpx.Response(200, json=REAL_SLOTS) if slots_ok else httpx.Response(501, json={})
        assert handler is not None
        return await handler(json.loads(request.content))

    return httpx.MockTransport(handle)


def _vllm(handler=None):
    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": MODEL, "owned_by": "vllm", "max_model_len": 98304}]})
        assert handler is not None
        return await handler(json.loads(request.content))

    return httpx.MockTransport(handle)


async def test_the_adapter_learns_the_pool_and_vllm_has_none():
    for provider in (Provider.VLLM, Provider.LLAMACPP):  # "vllm" in settings, llama.cpp in fact
        reset_endpoint_semaphores()
        a = OpenAICompatAdapter(ModelSpec(provider=provider, base_url="http://l/v1", model=MODEL))
        a._client = httpx.AsyncClient(transport=_llama(slots_ok=provider is Provider.VLLM))
        facts = await a.server()
        assert (facts.kind, facts.slots, facts.kv_pool) == (Provider.LLAMACPP, 4, 98304)
    reset_endpoint_semaphores()
    v = OpenAICompatAdapter(ModelSpec(provider=Provider.VLLM, base_url="http://v/v1", model=MODEL))
    v._client = httpx.AsyncClient(transport=_vllm())
    facts = await v.server()
    assert (facts.kind, facts.kv_pool) == (Provider.VLLM, None)


# --- admission by tokens ------------------------------------------------------------------------


async def test_a_call_that_would_overflow_the_pool_waits_until_the_first_ends():
    gate = EndpointGate(1, reserved=1, pool=98304)
    task = await gate.acquire(BACKGROUND, tokens=65_000 + 16_384)  # the scheduled call
    chat = asyncio.create_task(gate.acquire(TYPED, tokens=49_000 + 16_384))
    await asyncio.sleep(0.01)
    assert not chat.done(), "114k does not fit a 98k pool, reserved slot or not"
    await gate.release(task)
    hold = await asyncio.wait_for(chat, 1)
    assert gate.busy == 1 and hold.tokens == 65_384


async def test_two_small_calls_go_together_and_a_call_alone_always_goes():
    gate = EndpointGate(1, reserved=1, pool=98304)
    await gate.acquire(BACKGROUND, tokens=12_000 + 16_384)
    await asyncio.wait_for(gate.acquire(TYPED, tokens=9_000 + 16_384), 1)
    assert gate.busy == 2
    lone = EndpointGate(1, pool=98304)
    await asyncio.wait_for(lone.acquire(BACKGROUND, tokens=200_000), 1)  # num_ctx bounds it, not us


async def test_interactive_still_goes_first_when_the_pool_is_full():
    gate = EndpointGate(2, reserved=1, pool=98304)
    big = await gate.acquire(BACKGROUND, tokens=80_000)
    # A background call that does not fit waits; a chat call that does goes past it.
    bg_big = asyncio.create_task(gate.acquire(BACKGROUND, tokens=30_000))
    await asyncio.sleep(0)
    chat = await asyncio.wait_for(gate.acquire(TYPED, tokens=15_000), 1)
    assert not bg_big.done()
    await gate.release(chat)
    # A chat call that does not fit holds every background call behind it, even one that would.
    chat_big = asyncio.create_task(gate.acquire(TYPED, tokens=60_000))
    await asyncio.sleep(0)
    bg_small = asyncio.create_task(gate.acquire(BACKGROUND, tokens=1_000))
    await asyncio.sleep(0.01)
    assert not chat_big.done() and not bg_small.done(), "interactive first, FIFO within background"
    await gate.release(big)
    await asyncio.wait_for(chat_big, 1)
    hold = await asyncio.wait_for(bg_big, 1)  # 60k + 30k fit; background's turn in arrival order
    assert not bg_small.done(), "background room is the limit: 2 in flight"
    await gate.release(hold)
    await asyncio.wait_for(bg_small, 1)


async def test_a_lone_hold_keeps_the_endpoint_to_itself():
    gate = EndpointGate(2, reserved=1, pool=98304)
    first = await gate.acquire(BACKGROUND, tokens=1_000)
    alone = asyncio.create_task(gate.acquire(TYPED, tokens=1_000, alone=True))
    await asyncio.sleep(0.01)
    assert not alone.done()
    await gate.release(first)
    hold = await asyncio.wait_for(alone, 1)
    other = asyncio.create_task(gate.acquire(TYPED, tokens=1))
    await asyncio.sleep(0.01)
    assert not other.done(), "nothing joins a call that runs alone"
    await gate.release(hold)
    await asyncio.wait_for(other, 1)


def _sse(*events: dict) -> httpx.Response:
    lines = [f"data: {json.dumps(e)}" for e in events] + ["data: [DONE]"]
    return httpx.Response(200, content="\n".join(lines).encode())


def _ok(text: str = "ok") -> httpx.Response:
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})


async def _collect(adapter: OpenAICompatAdapter, text: str, urgency: int) -> str:
    u = call_urgency.set(urgency)
    c = current_conversation_id.set(text)
    try:
        out = []
        async for ev in adapter.stream([Message.user(text)], [], cancel=asyncio.Event()):
            if isinstance(ev, ModelTextChunk):
                out.append(ev.text)
        return "".join(out)
    finally:
        call_urgency.reset(u)
        current_conversation_id.reset(c)


def _pair(transport: httpx.MockTransport, provider: Provider = Provider.LLAMACPP, max_tokens: int = 1000):
    """A background and a chat adapter on one endpoint: limit 1, one reserved interactive call."""
    adapters = []
    for role in ("background", "chat"):
        spec = ModelSpec(provider=provider, base_url="http://l/v1", model=MODEL, max_tokens=max_tokens)
        a = OpenAICompatAdapter(spec, concurrency=1, reserved=1, role=role)
        a._client = httpx.AsyncClient(transport=transport)
        adapters.append(a)
    return adapters


async def test_the_adapter_holds_a_call_that_does_not_fit_beside_the_one_in_flight():
    task_may_end = asyncio.Event()
    log: list[str] = []

    async def handler(body: dict) -> httpx.Response:
        who = body["messages"][0]["content"]
        log.append(f"{who} sent")
        if who == "task":
            await task_may_end.wait()
        log.append(f"{who} done")
        return _ok(who)

    bg, chat = _pair(_llama(handler=handler), max_tokens=60_000)  # 2 x 60k > 98k
    await bg.server()
    task = asyncio.create_task(_collect(bg, "task", BACKGROUND))
    await asyncio.sleep(0.02)
    reply = asyncio.create_task(_collect(chat, "chat", TYPED))
    await asyncio.sleep(0.02)
    assert log == ["task sent"], "the reserve is a slot, not room in the pool"
    task_may_end.set()
    assert await asyncio.wait_for(reply, 2) == "chat" and await task == "task"
    assert log == ["task sent", "task done", "chat sent", "chat done"]


async def test_small_calls_still_run_side_by_side_on_llamacpp_and_vllm_admits_by_count_only():
    for transport_of, provider, max_tokens in ((_llama, Provider.LLAMACPP, 1000), (_vllm, Provider.VLLM, 60_000)):
        reset_endpoint_semaphores()
        task_may_end = asyncio.Event()
        log: list[str] = []

        async def handler(body: dict, log=log, task_may_end=task_may_end) -> httpx.Response:
            who = body["messages"][0]["content"]
            log.append(who)
            if who == "task":
                await task_may_end.wait()
            return _ok(who)

        bg, chat = _pair(transport_of(handler=handler), provider, max_tokens=max_tokens)
        assert (await bg.server()).kind is provider
        await bg.server()
        task = asyncio.create_task(_collect(bg, "task", BACKGROUND))
        await asyncio.sleep(0.02)
        assert await asyncio.wait_for(_collect(chat, "chat", TYPED), 2) == "chat", "the reserve works as before"
        task_may_end.set()
        await task
        assert log == ["task", "chat"]


# --- running out anyway ------------------------------------------------------------------------


async def test_a_crowded_overflow_before_any_output_goes_again_alone():
    """The estimate is chars, not tokens, and pictures are not in it: both calls were let in, the
    server ran out anyway. The chat waits for the task to end and goes again - the run never sees it."""
    task_may_end = asyncio.Event()
    log: list[str] = []
    attempts = {"chat": 0}

    async def handler(body: dict) -> httpx.Response:
        who = body["messages"][0]["content"]
        if who == "task":
            log.append("task sent")
            await task_may_end.wait()
            log.append("task done")
            return _ok(who)
        attempts["chat"] += 1
        log.append(f"chat #{attempts['chat']}")
        if attempts["chat"] == 1:
            task_may_end.set()  # the task finishes while the chat waits its turn alone
            return _sse({"error": OVERFLOW})
        return _ok("chat")

    bg, chat = _pair(_llama(handler=handler))
    await bg.server()
    task = asyncio.create_task(_collect(bg, "task", BACKGROUND))
    await asyncio.sleep(0.02)
    assert await asyncio.wait_for(_collect(chat, "chat", TYPED), 2) == "chat"
    await task
    assert log == ["task sent", "chat #1", "task done", "chat #2"]


async def test_a_crowded_overflow_after_output_tells_the_caller_and_a_lone_overflow_fails_as_before():
    task_may_end = asyncio.Event()

    async def handler(body: dict) -> httpx.Response:
        who = body["messages"][0]["content"]
        if who == "task":
            await task_may_end.wait()
            return _ok(who)
        return _sse({"choices": [{"delta": {"content": "half an ans"}}]}, {"error": OVERFLOW})

    bg, chat = _pair(_llama(handler=handler))
    await bg.server()
    task = asyncio.create_task(_collect(bg, "task", BACKGROUND))
    await asyncio.sleep(0.02)
    with pytest.raises(ModelError) as crowded:
        await asyncio.wait_for(_collect(chat, "chat", TYPED), 2)
    assert crowded.value.crowded and crowded.value.retryable
    task_may_end.set()
    await task
    # Alone on the endpoint: a real overflow, the error it always was.
    with pytest.raises(ModelError) as lone:
        await asyncio.wait_for(_collect(chat, "chat", TYPED), 2)
    assert not lone.value.crowded and "Context size has been exceeded" in str(lone.value)


async def test_vllm_overflow_beside_another_call_is_not_retried():
    task_may_end = asyncio.Event()
    posts: list[str] = []

    async def handler(body: dict) -> httpx.Response:
        who = body["messages"][0]["content"]
        posts.append(who)
        if who == "task":
            await task_may_end.wait()
            return _ok(who)
        return _sse({"error": OVERFLOW})

    bg, chat = _pair(_vllm(handler=handler), Provider.VLLM)
    await bg.server()
    task = asyncio.create_task(_collect(bg, "task", BACKGROUND))
    await asyncio.sleep(0.02)
    with pytest.raises(ModelError) as exc:
        await asyncio.wait_for(_collect(chat, "chat", TYPED), 2)
    assert not exc.value.crowded and posts == ["task", "chat"]
    task_may_end.set()
    await task


def test_ollama_shares_the_gate_without_a_pool():
    spec = ModelSpec(provider=Provider.OLLAMA, base_url="http://o", model="m")
    a = OllamaAdapter(spec, concurrency=1, reserved=1)
    assert a._sem is endpoint_semaphore(spec, 1, 1) and a._sem.pool is None
    assert spec.endpoint_key not in server_facts


async def test_the_run_loop_makes_a_crowded_step_again_alone(tmp_path):
    from tests.conftest import Harness

    h = Harness(tmp_path)
    await h.start()
    try:
        alone: list[bool] = []
        h.chat.on_call = lambda _n: alone.append(call_alone.get())
        h.chat.push(FakeTurn(text="half an ans", crowded_after_text=True), FakeTurn(text="the whole answer"))
        conv = await h.core.store.create_conversation()
        sub = h.subscribe(conv.id)
        await h.core.engine.create_run(text="hi", conversation_id=conv.id)
        seen = await h.wait_for(sub, "run.done", timeout=15)
        assert [e.role for e in seen if e.type == "model.call"] == ["chat", "chat (again, alone)"]
        assert alone == [False, True] and call_alone.get() is False
        msgs = await h.core.store.list_messages(conv.id)
        assert msgs[-1].content == "the whole answer"
    finally:
        await h.stop()
