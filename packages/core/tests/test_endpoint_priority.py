"""Time to first token on a shared endpoint (2026-11-16): who gets the next slot, the slots only
interactive calls may use, and each server's own speed levers in the request body."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis_core.engine.current import current_conversation_id
from jarvis_core.models.base import (
    BACKGROUND,
    TYPED,
    VOICE,
    EndpointGate,
    SlotPool,
    call_urgency,
    reset_endpoint_semaphores,
)
from jarvis_core.models.factory import AdapterFactory
from jarvis_core.models.ollama import OllamaAdapter
from jarvis_core.models.openai_compat import OpenAICompatAdapter
from jarvis_proto import Message, ModelSpec, Provider, RoleName, RunKind, Settings


@pytest.fixture(autouse=True)
def _fresh() -> None:
    reset_endpoint_semaphores()


# --- the gate: interactive first, FIFO within a class, a reserve background never takes ----------


async def test_interactive_calls_jump_the_queue_and_each_class_is_first_come_first_served():
    gate = EndpointGate(1)
    order: list[str] = []
    await gate.acquire(BACKGROUND)  # a task's step holds the only slot

    async def wants(name: str, urgency: int) -> None:
        await gate.acquire(urgency)
        order.append(name)
        await gate.release()

    waiters = []
    for name, urgency in (("bg1", BACKGROUND), ("chat1", TYPED), ("bg2", BACKGROUND), ("chat2", TYPED)):
        waiters.append(asyncio.create_task(wants(name, urgency)))
        await asyncio.sleep(0)
    await gate.release()
    await asyncio.wait_for(asyncio.gather(*waiters), 1)
    assert order == ["chat1", "chat2", "bg1", "bg2"]


async def test_a_reserved_slot_lets_chat_start_beside_a_long_background_call():
    gate = EndpointGate(1, reserved=1)
    await gate.acquire(BACKGROUND)  # the 5-minute task call
    await asyncio.wait_for(gate.acquire(TYPED), 1)  # no waiting: the reserve is the chat's
    assert gate.busy == 2
    second_bg = asyncio.create_task(gate.acquire(BACKGROUND))
    await asyncio.sleep(0.01)
    assert not second_bg.done(), "background never takes the reserve"
    # A voice call still gets one more on top of the reserve.
    await asyncio.wait_for(gate.acquire(VOICE), 1)
    assert gate.busy == 3
    third = asyncio.create_task(gate.acquire(TYPED))
    await asyncio.sleep(0.01)
    assert not third.done(), "typed room is limit + reserved"
    await gate.release()
    await gate.release()
    await asyncio.wait_for(third, 1)
    for _ in range(2):
        await gate.release()
    await asyncio.wait_for(second_bg, 1)


async def test_without_a_reserve_the_gate_behaves_as_before():
    gate = EndpointGate(2, reserved=0)
    assert (gate.room(BACKGROUND), gate.room(TYPED), gate.room(VOICE)) == (2, 2, 3)
    gate = EndpointGate(1, reserved=2)
    assert (gate.room(BACKGROUND), gate.room(TYPED), gate.room(VOICE)) == (1, 3, 4)


async def test_the_run_engine_takes_interactive_kinds_from_settings(tmp_path):
    from jarvis_core.models.fake import FakeTurn
    from tests.conftest import Harness

    h = Harness(tmp_path)
    await h.start()
    try:
        seen: list[int] = []
        h.chat.on_call = lambda _n: seen.append(call_urgency.get())
        for kinds, kind in (([RunKind.CHAT], RunKind.SCHEDULED), ([RunKind.SCHEDULED], RunKind.SCHEDULED)):
            h.enable(interactive_run_kinds=kinds)
            h.chat.push(FakeTurn(text="done"))
            _, conv = await h.core.engine.create_run(text="report", kind=kind)
            await h.wait_for(h.subscribe(conv.id), "run.done")
        assert seen[0] == BACKGROUND and seen[-1] == TYPED
    finally:
        await h.stop()


def test_interactive_kinds_and_reserve_round_trip_through_settings():
    s = Settings.model_validate({"interactive_run_kinds": ["chat", "task"], "interactive_reserved_calls": 2})
    back = Settings.model_validate_json(s.model_dump_json())
    assert back.interactive_run_kinds == [RunKind.CHAT, RunKind.TASK]
    assert back.interactive_reserved_calls == 2
    d = Settings()
    assert d.interactive_run_kinds == [RunKind.CHAT, RunKind.COLLAB] and d.interactive_reserved_calls == 1
    spec = ModelSpec.model_validate(
        {"provider": "llamacpp", "slot_pinning": False, "interactive_slots": 3, "priority_scheduling": True}
    )
    again = ModelSpec.model_validate_json(spec.model_dump_json())
    assert (again.provider, again.slot_pinning, again.interactive_slots, again.priority_scheduling) == (
        Provider.LLAMACPP,
        False,
        3,
        True,
    )


def test_the_factory_hands_the_reserve_to_every_adapter():
    s = Settings(max_concurrent_runs_per_endpoint=1, interactive_reserved_calls=2)
    s.roles[RoleName.CHAT] = ModelSpec(provider=Provider.LLAMACPP, base_url="http://l/v1", model="m")
    adapter = AdapterFactory(s).for_role(RoleName.CHAT)
    assert isinstance(adapter, OpenAICompatAdapter)
    assert (adapter._sem.limit, adapter._sem.reserved) == (1, 2)


# --- llama.cpp slots ---------------------------------------------------------------------------


def test_slot_pool_keeps_classes_apart_and_goes_back_to_the_same_slot():
    pool = SlotPool(4, 2)
    chat = pool.acquire(True, ("c1", "chat"))
    planner = pool.acquire(True, ("c1", "planner"))
    task = pool.acquire(False, ("t1", "background"))
    assert {chat, planner} == {0, 1} and task in (2, 3)
    for s in (chat, planner, task):
        pool.release(s)
    assert pool.acquire(True, ("c1", "chat")) == chat, "a conversation's main loop finds its prefix again"
    assert pool.acquire(False, ("t2", "background")) in (2, 3)


def test_slot_pool_shares_a_busy_slot_of_its_own_class_rather_than_taking_the_others():
    pool = SlotPool(4, 1)
    assert pool.acquire(True, "a") == 0
    assert pool.acquire(True, "b") == 0, "interactive never spills into background slots"
    assert pool.acquire(False, "c") in (1, 2, 3)
    pool = SlotPool(1, 1)
    assert pool.acquire(True, "a") == 0  # degenerate: one slot, one class


def _sse_ok() -> httpx.Response:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    return httpx.Response(200, content="\n".join(lines).encode())


def _server(owner: str, *, slots: int | None = 4, seen: list[dict] | None = None, refuse_priority: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m", "owned_by": owner}]})
        if request.method == "GET" and path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": slots} if slots else {})
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        if refuse_priority and "priority" in body:
            msg = "Got priority 2 but Priority scheduling is not enabled."
            return httpx.Response(400, content=json.dumps({"error": {"message": msg}}).encode())
        return _sse_ok()

    return httpx.MockTransport(handler)


async def _call(adapter: OpenAICompatAdapter, urgency: int, conversation: str = "c1") -> None:
    await adapter.server()  # what a first call starts in the background, settled here
    u = call_urgency.set(urgency)
    c = current_conversation_id.set(conversation)
    try:
        async for _ in adapter.stream([Message.user("hi")], [], cancel=asyncio.Event()):
            pass
    finally:
        call_urgency.reset(u)
        current_conversation_id.reset(c)


async def test_llamacpp_pins_interactive_and_background_calls_to_their_own_slots():
    seen: list[dict] = []
    spec = ModelSpec(provider=Provider.LLAMACPP, base_url="http://l/v1", model="m", interactive_slots=1)
    adapter = OpenAICompatAdapter(spec, role="chat")
    adapter._client = httpx.AsyncClient(transport=_server("llamacpp", seen=seen))
    await _call(adapter, TYPED)
    await _call(adapter, VOICE)
    await _call(adapter, BACKGROUND, "task")
    assert [b["id_slot"] for b in seen] == [0, 0, 1]
    assert all(b["cache_prompt"] is True for b in seen)
    assert "priority" not in seen[0]


async def test_a_vllm_provider_that_is_really_llamacpp_is_detected():
    seen: list[dict] = []
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://l/v1", model="m")
    adapter = OpenAICompatAdapter(spec, role="chat")
    adapter._client = httpx.AsyncClient(transport=_server("llamacpp", seen=seen))
    await _call(adapter, BACKGROUND)
    assert seen[0]["id_slot"] == 2 and seen[0]["cache_prompt"] is True  # default 2 interactive of 4


async def test_pinning_off_or_unknown_slot_count_sends_no_slot():
    seen: list[dict] = []
    spec = ModelSpec(provider=Provider.LLAMACPP, base_url="http://l/v1", model="m", slot_pinning=False)
    adapter = OpenAICompatAdapter(spec)
    adapter._client = httpx.AsyncClient(transport=_server("llamacpp", seen=seen))
    await _call(adapter, TYPED)
    reset_endpoint_semaphores()
    spec2 = ModelSpec(provider=Provider.LLAMACPP, base_url="http://l/v1", model="m")
    adapter2 = OpenAICompatAdapter(spec2)
    adapter2._client = httpx.AsyncClient(transport=_server("llamacpp", slots=None, seen=seen))
    await _call(adapter2, TYPED)
    assert ["id_slot" in b for b in seen] == [False, False]
    assert all(b["cache_prompt"] is True for b in seen)


async def test_vllm_sends_nothing_extra_unless_it_schedules_by_priority():
    seen: list[dict] = []
    plain = OpenAICompatAdapter(ModelSpec(provider=Provider.VLLM, base_url="http://v/v1", model="m"))
    plain._client = httpx.AsyncClient(transport=_server("vllm", seen=seen))
    await _call(plain, BACKGROUND)
    assert not {"id_slot", "cache_prompt", "priority"} & set(seen[0])
    reset_endpoint_semaphores()
    seen.clear()
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://v/v1", model="m", priority_scheduling=True)
    prio = OpenAICompatAdapter(spec)
    prio._client = httpx.AsyncClient(transport=_server("vllm", seen=seen))
    await _call(prio, TYPED)
    await _call(prio, BACKGROUND)
    assert [b["priority"] for b in seen] == [TYPED, BACKGROUND]
    assert "id_slot" not in seen[0]


async def test_vllm_that_refuses_priority_gets_the_call_again_without_it_and_never_again():
    seen: list[dict] = []
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://v/v1", model="m", priority_scheduling=True)
    adapter = OpenAICompatAdapter(spec)
    adapter._client = httpx.AsyncClient(transport=_server("vllm", seen=seen, refuse_priority=True))
    await _call(adapter, BACKGROUND)
    await _call(adapter, BACKGROUND)
    assert ["priority" in b for b in seen] == [True, False, False]


# --- Ollama -------------------------------------------------------------------------------------


def test_ollama_keeps_the_model_loaded_and_one_num_ctx_per_box():
    s = Settings()
    s.roles[RoleName.CHAT] = ModelSpec(provider=Provider.OLLAMA, base_url="http://o", model="m", num_ctx=32768)
    s.roles[RoleName.PLANNER] = ModelSpec(provider=Provider.OLLAMA, base_url="http://o", model="m", num_ctx=None)
    planner = AdapterFactory(s).for_role(RoleName.PLANNER)  # outside any run: its own spec
    assert isinstance(planner, OllamaAdapter)
    body = planner._payload([Message.user("hi")], [])
    assert body["options"]["num_ctx"] == 32768, "the same box never sees a second num_ctx"
    assert body["keep_alive"] == "30m"
    assert not {"id_slot", "cache_prompt", "priority"} & set(body)
    own = OllamaAdapter(ModelSpec(provider=Provider.OLLAMA, base_url="http://o", model="m", keep_alive="-1"))
    assert own._payload([Message.user("hi")], [])["keep_alive"] == "-1"


async def test_the_first_call_does_not_wait_for_the_look_at_the_server():
    seen: list[dict] = []
    spec = ModelSpec(provider=Provider.VLLM, base_url="http://l/v1", model="m")
    adapter = OpenAICompatAdapter(spec)
    adapter._client = httpx.AsyncClient(transport=_server("llamacpp", seen=seen))
    u = call_urgency.set(TYPED)
    try:
        async for _ in adapter.stream([Message.user("hi")], [], cancel=asyncio.Event()):
            pass
        await asyncio.sleep(0.05)  # the look finishes in the background
        async for _ in adapter.stream([Message.user("hi")], [], cancel=asyncio.Event()):
            pass
    finally:
        call_urgency.reset(u)
    assert "id_slot" not in seen[0] and seen[1]["id_slot"] == 0
