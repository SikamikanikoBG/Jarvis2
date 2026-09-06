"""The loop under a scripted model: happy path, tools, validation, empty replies, budgets."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import RunKind, RunStatus, ToolCall, ToolResult, ToolResultKind
from tests.conftest import Harness


class _EchoArgs(BaseModel):
    text: str


class ToolsForTests(BuiltinProvider):
    calls: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    @tool("test.echo", description="echo", args=_EchoArgs, read_only=True)
    async def _echo(self, text: str) -> ToolResult:
        self.calls.append(text)
        return ToolResult.data(f"echo:{text}")

    @tool("test.fail", description="always fails", read_only=True)
    async def _fail(self) -> ToolResult:
        return ToolResult.failure("boom")

    @tool("test.slow", description="slow", read_only=True)
    async def _slow(self, cancel: asyncio.Event) -> ToolResult:
        await asyncio.wait_for(cancel.wait(), timeout=10)
        return ToolResult.data("never")

    @tool("test.send", description="mutating", destructive=True)
    async def _send(self) -> ToolResult:
        self.calls.append("send")
        return ToolResult.data("sent")

    @tool("test.write", description="mutating, not destructive")
    async def _write(self) -> ToolResult:
        self.calls.append("write")
        return ToolResult.data("written")


async def with_tools(h: Harness) -> ToolsForTests:
    t = ToolsForTests()
    h.core.registry.add(t)
    await h.core.registry.refresh()
    return t


async def test_plain_reply_is_streamed_and_persisted(harness: Harness):
    harness.chat.push(FakeTurn(text="Hello Arsen, all good.", reasoning="thinking a bit"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="hi", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    types = [e.type for e in seen]
    assert "model.call" in types and types.count("model.delta") >= 4
    assert any(e.type == "model.delta" and e.kind == "reasoning" for e in seen)
    msgs = await harness.core.store.list_messages(conv.id)
    assert [m.role.value for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "Hello Arsen, all good." and msgs[1].reasoning == "thinking a bit"
    done = await harness.core.store.get_run(run.id)
    assert done is not None and done.status is RunStatus.DONE and done.usage.calls == 1
    # Deltas are never persisted; the rest is.
    stored = [e["type"] for e in await harness.core.store.list_events(run.id)]
    assert "model.delta" not in stored and stored[-1] == "run.done"
    # The user message carries the run id so resume can find it.
    assert msgs[0].run_id == run.id


async def test_tool_call_roundtrip_and_system_prompt(harness: Harness):
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"text": "x"})]),
        FakeTurn(text="done: echo:x"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="echo x", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert tools.calls == ["x"]
    tr = next(e for e in seen if e.type == "tool.result")
    assert tr.result.kind is ToolResultKind.DATA and tr.result.text == "echo:x"
    # Second model call saw the tool message and the system prompt came first.
    second = harness.chat.calls[1][0]
    assert second[0].role.value == "system" and "PRECEDENCE" in second[0].content
    assert second[-1].role.value == "tool" and second[-1].content == "echo:x"
    assert any(t.name == "test.echo" for t in harness.chat.calls[0][1])


async def test_invalid_arguments_go_back_to_the_model_as_an_error(harness: Harness):
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"wrong": 1})]),
        FakeTurn(text="ok"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    tr = next(e for e in seen if e.type == "tool.result")
    assert tr.result.kind is ToolResultKind.ERROR and "invalid arguments" in tr.result.text
    assert tools.calls == []  # never executed


async def test_unknown_tool_is_an_error_result(harness: Harness):
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="nope.x", arguments={})]),
        FakeTurn(text="ok"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    tr = next(e for e in seen if e.type == "tool.result")
    assert "unknown tool" in tr.result.text


async def test_empty_reply_is_nudged_once_then_fails(harness: Harness):
    harness.chat.push(FakeTurn(text=""), FakeTurn(text=""))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.failed")
    types = [e.type for e in seen]
    assert types.count("guard.armed") == 1 and types.count("guard.consumed") == 1
    assert "empty" in seen[-1].error


async def test_step_budget_ends_the_run_with_a_summary(harness: Harness):
    await with_tools(harness)
    # Always call a tool with different args → no repetition signal, only the budget.
    for i in range(30):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": str(i)})]))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    s = harness.core.settings.model_copy(deep=True)
    s.budgets[RunKind.CHAT].max_steps = 4
    harness.core.apply_settings(s)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    done = seen[-1]
    assert done.summary and "step budget" in done.summary and done.steps_used == 4
    msgs = await harness.core.store.list_messages(conv.id)
    assert "I stopped here" in msgs[-1].content


async def test_repeated_identical_call_summons_the_judge_who_stops_it(harness: Harness):
    await with_tools(harness)
    for i in range(10):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": "same"})]))
    harness.judge.push(FakeTurn(text='{"verdict": "stop", "reason": "looping on the same call"}'))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    types = [e.type for e in seen]
    assert "guard.armed" in types and "judge.verdict" in types and "guard.consumed" in types
    verdict = next(e for e in seen if e.type == "judge.verdict")
    assert verdict.verdict == "stop"
    assert types.count("model.call") == 3  # threshold reached on the third identical call
    assert "supervisor" in seen[-1].summary


async def test_judge_nudge_changes_course_and_is_capped(harness: Harness):
    await with_tools(harness)
    for i in range(10):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": "same"})]))
    harness.judge.push(
        FakeTurn(text='{"verdict": "nudge", "reason": "try something else"}'),
        FakeTurn(text='{"verdict": "nudge", "reason": "still the same"}'),
        FakeTurn(text='{"verdict": "nudge", "reason": "third nudge would be too many"}'),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    verdicts = [e.verdict for e in seen if e.type == "judge.verdict"]
    assert verdicts == ["nudge", "nudge", "stop"]
    msgs = await harness.core.store.list_messages(conv.id)
    assert sum(1 for m in msgs if m.name == "supervisor") == 2


async def test_error_streak_summons_the_judge(harness: Harness):
    await with_tools(harness)
    for i in range(6):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.fail", arguments={})]))
    harness.judge.push(FakeTurn(text='{"verdict": "stop", "reason": "tool keeps failing"}'))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    armed = next(e for e in seen if e.type == "guard.armed")
    assert "consecutive tool errors" in armed.detail or "repeated identical call" in armed.detail


async def test_judge_unavailable_stops_the_run_rather_than_letting_it_loop(harness: Harness):
    await with_tools(harness)
    for i in range(10):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": "same"})]))
    harness.judge.push(FakeTurn(fail_before_first_byte=True))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    verdict = next(e for e in seen if e.type == "judge.verdict")
    assert verdict.verdict == "stop" and "could not evaluate" in verdict.reason


async def test_model_failure_fails_the_run_loudly(harness: Harness):
    harness.chat.push(FakeTurn(fail_before_first_byte=True))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.failed")
    assert "connection refused" in seen[-1].error


async def test_destructive_tool_waits_for_confirmation_then_runs(harness: Harness):
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]),
        FakeTurn(text="sent it"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="send", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert tools.calls == []
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.WAITING_USER
    assert await harness.core.engine.confirm(run.id, req.call_id, True)
    seen = await harness.wait_for(sub, "run.done")
    assert tools.calls == ["send"]
    assert any(e.type == "tool.confirm_resolved" and e.approved for e in seen)


async def test_destructive_tool_rejected_is_reported_to_the_model(harness: Harness):
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]),
        FakeTurn(text="understood, not sent"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="send", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    await harness.core.engine.confirm(run.id, req.call_id, False, "not now")
    await harness.wait_for(sub, "run.done")
    assert tools.calls == []
    tool_msg = harness.chat.calls[1][0][-1]
    assert "rejected by user (not now)" in tool_msg.content


async def test_unattended_runs_do_not_ask_for_confirmation(harness: Harness):
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]),
        FakeTurn(text="sent"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="send", conversation_id=conv.id, kind=RunKind.SCHEDULED)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    assert tools.calls == ["send"]
    assert not any(e.type == "tool.confirm_requested" for e in seen)


async def test_mutating_call_gets_an_idempotency_key(harness: Harness):
    await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.write", arguments={})]),
        FakeTurn(text="ok"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="w", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    call = next(e for e in seen if e.type == "tool.call")
    assert call.idempotency_key == f"{run.id}:c1" and call.read_only is False
    assert await harness.core.store.idempotent_result(call.idempotency_key) is not None


async def test_a_repeated_mutating_call_is_answered_with_what_it_did(harness: Harness):
    """A model that re-emits a tool_call id must not be told its action failed.

    The key is claimed once, so the second call is refused — and the refusal used to be all the
    model got ("duplicate call refused"), which reads as a failure. The result had been recorded
    all along by record_idempotent_result and nothing ever read it back; a model that believes
    its mail was not sent sends it again by another route.
    """
    tools = await with_tools(harness)
    repeated = [ToolCall(id="same", name="test.write", arguments={})]
    harness.chat.push(
        FakeTurn(tool_calls=list(repeated)),
        FakeTurn(tool_calls=list(repeated)),  # the same call id again
        FakeTurn(text="ok"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="w", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=10)

    assert tools.calls == ["write"], "the action must run exactly once"
    tool_msgs = [m for m in await harness.core.store.list_messages(conv.id) if m.role.value == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0].content == "written"
    # The replay says what happened, and says that it is a replay.
    assert tool_msgs[1].content.startswith("written")
    assert "already ran in this run" in tool_msgs[1].content
    assert "Error" not in tool_msgs[1].content


async def test_old_tool_results_are_truncated_in_context_but_kept_in_db(harness: Harness):
    """A 5k-char tool result is whole for the step that must read it, a head afterwards."""
    tools = await with_tools(harness)
    big = "row " * 1500  # ~6k chars
    # Budget-based: two 6k results must exceed the budget for the older one to be truncated.
    harness.enable(tool_context_token_budget=2_500)  # ~8k chars

    async def big_echo(text: str = "") -> ToolResult:
        return ToolResult.data(big)

    tools._entries["test.echo"].fn = big_echo
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"text": "a"})]),
        FakeTurn(tool_calls=[ToolCall(id="c2", name="test.echo", arguments={"text": "b"})]),
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="two big reads", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=10)
    # Call 2 (answering the first tool result) saw it in full.
    second_call_msgs = harness.chat.calls[1][0]
    first_result_at_call2 = next(m for m in second_call_msgs if m.role.value == "tool" and m.tool_call_id == "c1")
    assert len(first_result_at_call2.content) > 5000
    # Call 3 saw the first result truncated and the second (fresh) result whole.
    third_call_msgs = harness.chat.calls[2][0]
    first_at_call3 = next(m for m in third_call_msgs if m.role.value == "tool" and m.tool_call_id == "c1")
    second_at_call3 = next(m for m in third_call_msgs if m.role.value == "tool" and m.tool_call_id == "c2")
    assert "[truncated to save context" in first_at_call3.content and len(first_at_call3.content) < 1000
    assert len(second_at_call3.content) > 5000
    # The database keeps the full text.
    stored = [m for m in await harness.core.store.list_messages(conv.id) if m.role.value == "tool"]
    assert all(len(m.content) > 5000 and "[truncated" not in m.content for m in stored)


async def test_tool_results_under_budget_are_never_truncated(harness: Harness):
    """The read-many workflow: while results fit the budget, every body stays whole."""
    tools = await with_tools(harness)
    body = "mail body " * 300  # ~3k chars each

    async def read(text: str = "") -> ToolResult:
        return ToolResult.data(f"{text}: {body}")

    tools._entries["test.echo"].fn = read
    for i in range(5):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": f"mail{i}"})]))
    harness.chat.push(FakeTurn(text="summary of 5 mails"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="summarise the mails", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=10)
    last_call_msgs = harness.chat.calls[-1][0]
    tool_msgs = [m for m in last_call_msgs if m.role.value == "tool"]
    assert len(tool_msgs) == 5 and all("[truncated" not in m.content for m in tool_msgs)
