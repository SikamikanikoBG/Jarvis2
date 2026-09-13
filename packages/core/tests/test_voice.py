"""A call with Jarvis (docs/stories/10_voice.md): the same conversation, one more channel.

What the core owes a voice run, checked one promise at a time: the words are marked as spoken on
both sides, the model is told it is on a call in the per-turn context (never in the cached
prefix), it is offered only the tools a call may use and refuses the others by name, it does not
plan, it does not think, and a cut-in that arrives late becomes a voice run of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import Channel, RoleName, ToolCall, VoiceSettings
from tests.conftest import Harness


async def test_a_voice_turn_is_spoken_on_both_sides_and_briefed_in_the_turn_context(harness: Harness):
    core = harness.core
    harness.chat.push(FakeTurn(text="Три дни, без отделяне. Има ли бучка?"))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="сърбежът е от три дни", conversation_id=conv.id, channel=Channel.VOICE)
    seen = await harness.wait_for(sub, "run.done")

    stored = await core.store.get_run(run.id)
    assert stored is not None and stored.channel is Channel.VOICE
    # Thinking is off for the call: it is what the model was actually asked, not a field.
    call = next(e for e in seen if e.type == "model.call")
    assert call.think is False
    msgs = await core.store.list_messages(conv.id)
    by_role = {(m.role.value, m.name): m for m in msgs}
    assert by_role[("user", None)].channel is Channel.VOICE
    assert by_role[("assistant", None)].channel is Channel.VOICE
    # The context note is a system note, not something anyone said: it stays "text".
    assert by_role[("user", "context")].channel is Channel.TEXT

    sent = harness.chat.calls[0][0]
    system = sent[0]
    assert system.role.value == "system" and "Voice call" not in system.content, "the cached prefix is untouched"
    context = [m for m in sent if m.name == "context"]
    assert context and "## Voice call" in context[0].content and "read aloud" in context[0].content


async def test_a_typed_turn_carries_no_voice_block_and_is_marked_text(harness: Harness):
    core = harness.core
    harness.chat.push(FakeTurn(text="Fine."))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="all good?", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    sent = harness.chat.calls[0][0]
    assert not any("## Voice call" in m.content for m in sent)
    assert all(m.channel is Channel.TEXT for m in await core.store.list_messages(conv.id))


async def test_on_a_call_he_is_offered_only_the_namespaces_a_call_may_use(harness: Harness):
    core = harness.core
    harness.chat.push(FakeTurn(text="Noted."))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(
        text="запомни: Мими предпочита планина", conversation_id=conv.id, channel=Channel.VOICE
    )
    await harness.wait_for(sub, "run.done")
    offered = {t.name for t in harness.chat.calls[0][1]}
    assert offered, "the default namespaces are still tools, not nothing"
    assert all(n.split(".", 1)[0] in VoiceSettings().namespaces for n in offered), sorted(offered)
    assert "notes.add" in offered  # he remembers
    assert not any(n.startswith("schedule.") or n.startswith("web.") or n.startswith("fetch.") for n in offered)


async def test_a_tool_outside_the_call_policy_is_refused_by_name_not_run(harness: Harness):
    core = harness.core
    harness.chat.push(
        FakeTurn(
            tool_calls=[ToolCall(id="c1", name="schedule.create", arguments={"cron": "0 9 * * 1", "prompt": "check"})]
        ),
        FakeTurn(text="Ще го направя след разговора, кажи ми го в чата."),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="пусни го всяка седмица", conversation_id=conv.id, channel=Channel.VOICE)
    seen = await harness.wait_for(sub, "run.done")
    results = [e for e in seen if e.type == "tool.result"]
    assert results and results[0].result.kind.value == "error" and "on a call" in results[0].result.text
    assert await core.db.fetchall("SELECT id FROM schedules") == []


async def test_a_call_never_plans_even_with_planning_on(harness: Harness):
    core = harness.core
    harness.enable(planning_enabled=True)
    # With planning on, a text run would first spend a model call on pre-flight. A voice run
    # goes straight to the answer: one scripted turn is all it may consume.
    harness.chat.push(FakeTurn(text="Първо да уточним обхвата."))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(
        text="Collect this week's numbers from the homelab and write the weekly report for Rumen with three sections.",
        conversation_id=conv.id,
        channel=Channel.VOICE,
    )
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    assert not any(e.type == "plan.created" for e in seen)
    assert len(harness.chat.calls) == 1


async def test_voice_think_setting_lets_a_call_reason_when_asked(harness: Harness):
    core = harness.core
    harness.enable(voice=VoiceSettings(think=True))
    harness.chat.push(FakeTurn(text="Hm."))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="think about it", conversation_id=conv.id, channel=Channel.VOICE)
    assert run.think is None, "left to the chat role's own setting"
    await harness.wait_for(sub, "run.done")


# --- WS: the channel rides on run.create and run.steer ------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        c.fake = fake  # type: ignore[attr-defined]
        yield c


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_ws_voice_create_and_a_late_cut_in_are_both_spoken(client: TestClient):
    client.fake.push(FakeTurn(text="Казвай."), FakeTurn(text="Разбрах, продължавай."))  # type: ignore[attr-defined]
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_text(json.dumps({"type": "run.create", "text": "здравей", "channel": "voice"}))
        run_id = None
        while True:
            ev = json.loads(ws.receive_text())
            if ev["type"] == "run.queued":
                run_id = ev["run_id"]
            if ev["type"] == "run.done":
                break
        assert run_id
        # The run is over; the cut-in arrives late and becomes a voice run of its own.
        ws.send_text(json.dumps({"type": "run.steer", "run_id": run_id, "text": "и още нещо", "channel": "voice"}))
        while json.loads(ws.receive_text())["type"] != "run.done":
            pass
    run = client.get(f"/api/runs/{run_id}", headers=_h()).json()
    msgs = client.get(f"/api/conversations/{run['conversation_id']}/messages", headers=_h()).json()
    spoken = [m for m in msgs if m["channel"] == "voice"]
    assert [m["role"] for m in spoken] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in spoken if m["role"] == "user"] == ["здравей", "и още нещо"]
