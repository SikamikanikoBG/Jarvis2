"""Thinking on/off and level: settings, adapters, per-run override."""

from __future__ import annotations

from jarvis_core.models.factory import AdapterFactory
from jarvis_core.models.fake import FakeTurn
from jarvis_core.models.ollama import OllamaAdapter
from jarvis_core.models.openai_compat import OpenAICompatAdapter
from jarvis_proto import Message, ModelSpec, Provider, RoleName, Settings
from tests.conftest import Harness


def test_level_is_dropped_when_thinking_is_off():
    assert ModelSpec(think=False, think_level="high").think_level is None
    assert ModelSpec(think=True, think_level="high").think_level == "high"


def test_with_thinking_override():
    base = ModelSpec(think=True, think_level="medium")
    assert base.with_thinking(None, None) is base
    off = base.with_thinking(False, None)
    assert off.think is False and off.think_level is None
    hi = base.with_thinking(None, "high")
    assert hi.think and hi.think_level == "high"
    on = ModelSpec(think=False).with_thinking(True, "low")
    assert on.think and on.think_level == "low"


def test_ollama_sends_level_as_think_string_and_vllm_as_reasoning_effort():
    o = OllamaAdapter(
        ModelSpec(provider=Provider.OLLAMA, base_url="http://x", model="m", think=True, think_level="high")
    )
    assert o._payload([Message.user("q")], [])["think"] == "high"
    o2 = OllamaAdapter(ModelSpec(provider=Provider.OLLAMA, base_url="http://x", model="m", think=True))
    assert o2._payload([Message.user("q")], [])["think"] is True
    v = OpenAICompatAdapter(
        ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=True, think_level="low")
    )
    p = v._payload([Message.user("q")], [])
    assert p["chat_template_kwargs"] == {"enable_thinking": True} and p["reasoning_effort"] == "low"
    v2 = OpenAICompatAdapter(ModelSpec(provider=Provider.VLLM, base_url="http://x/v1", model="m", think=False))
    p2 = v2._payload([Message.user("q")], [])
    assert p2["chat_template_kwargs"] == {"enable_thinking": False} and "reasoning_effort" not in p2


def test_factory_override_yields_sibling_adapter():
    f = AdapterFactory(Settings())
    a = f.for_role(RoleName.CHAT)
    b = f.for_role(RoleName.CHAT, think=False)
    c = f.for_role(RoleName.CHAT, think_level="high")
    assert a.spec.think and not b.spec.think and c.spec.think_level == "high"
    assert a is not b and f.for_role(RoleName.CHAT) is a


async def test_per_run_override_reaches_the_model_call(harness: Harness):
    harness.chat.push(FakeTurn(text="a"), FakeTurn(text="b"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="no thinking please", conversation_id=conv.id, think=False)
    seen = await harness.wait_for(sub, "run.done")
    call = next(e for e in seen if e.type == "model.call")
    assert call.think is False and call.think_level is None
    await harness.core.engine.create_run(text="deep", conversation_id=conv.id, think=True, think_level="high")
    seen = await harness.wait_for(sub, "run.done")
    call = next(e for e in seen if e.type == "model.call")
    assert call.think is True and call.think_level == "high"
