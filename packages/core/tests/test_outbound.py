"""Actions that speak for Arsen to someone else are checked against what he asked (review
2026-10-19: asked to get a public file deleted, Jarvis mailed the host's admin from his Gmail
through the browser and offered to pay a fee - the e-mail policy never saw it)."""

from __future__ import annotations

from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import Channel, Confirmations, RoleName, RunKind, RunStatus, ToolCall
from tests.conftest import Harness
from tests.test_loop import with_tools


def _checker(harness: Harness, *answers: str) -> FakeAdapter:
    checker = FakeAdapter([FakeTurn(text=a) for a in answers])
    harness.core.adapters.fakes[RoleName.CLASSIFIER] = checker
    return checker


def _autonomous(harness: Harness) -> None:
    harness.enable(confirmations=Confirmations(mode="off", always_ask=[], outbound_check=["test.write"]))


async def test_an_outbound_action_he_did_not_ask_for_waits_for_him(harness: Harness):
    tools = await with_tools(harness)
    _autonomous(harness)
    checker = _checker(
        harness, '{"outbound": true, "asked": false, "what": "e-mails admin@host.example offering to pay"}'
    )
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.write", arguments={})]),
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="get that file deleted now", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert "admin@host.example" in req.reason and tools.calls == []
    # The checker saw his request and the action.
    prompt = checker.calls[0][0][-1].content
    assert "get that file deleted now" in prompt and "test.write" in prompt
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.WAITING_USER
    await harness.core.engine.confirm(run.id, req.call_id, True)
    await harness.wait_for(sub, "run.done")
    assert tools.calls == ["write"]


async def test_an_outbound_action_he_asked_for_goes_ahead(harness: Harness):
    tools = await with_tools(harness)
    _autonomous(harness)
    _checker(harness, '{"outbound": true, "asked": true, "what": "posts the comment he dictated"}')
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.write", arguments={})]), FakeTurn(text="ok"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="post this comment: nice work", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert tools.calls == ["write"] and not any(e.type == "tool.confirm_requested" for e in seen)


async def test_tools_off_the_list_and_unattended_runs_are_not_checked(harness: Harness):
    tools = await with_tools(harness)
    _autonomous(harness)
    checker = _checker(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]),  # not on the list
        FakeTurn(text="ok"),
        FakeTurn(tool_calls=[ToolCall(id="c2", name="test.write", arguments={})]),  # a schedule's own job
        FakeTurn(text="ok"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    await harness.core.engine.create_run(text="daily post", conversation_id=conv.id, kind=RunKind.SCHEDULED)
    await harness.wait_for(sub, "run.done")
    assert tools.calls == ["send", "write"] and checker.calls == []


async def test_a_checker_that_cannot_answer_asks_rather_than_lets_it_through(harness: Harness):
    tools = await with_tools(harness)
    _autonomous(harness)
    _checker(harness, "no idea")
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.write", arguments={})]), FakeTurn(text="ok"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    assert any(e.type == "tool.confirm_requested" for e in seen) and tools.calls == []


async def test_on_a_call_he_is_asked_aloud(harness: Harness):
    tools = await with_tools(harness)
    _autonomous(harness)
    _checker(harness, '{"outbound": true, "asked": false, "what": "sends a mail to a stranger"}')
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.write", arguments={})]), FakeTurn(text="Shall I?")
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="sort it out", conversation_id=conv.id, channel=Channel.VOICE)
    seen = await harness.wait_for(sub, "run.done")
    result = next(e for e in seen if e.type == "tool.result")
    assert "go-ahead" in result.result.text and tools.calls == []
