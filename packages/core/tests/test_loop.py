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
        self.gate = asyncio.Event()
        self.in_flight = 0
        self.both_in_flight = asyncio.Event()

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

    @tool("test.gate", description="blocks until the test opens it", read_only=True)
    async def _gate(self) -> ToolResult:
        """A tool that is genuinely in flight until the test says otherwise — without cancelling
        the run, which is what makes it usable for testing anything mid-run."""
        self.calls.append("gate")
        await asyncio.wait_for(self.gate.wait(), timeout=10)
        return ToolResult.data("gate opened")

    @tool("test.barrier", description="read-only; only returns once two are in flight", read_only=True)
    async def _barrier(self) -> ToolResult:
        """Proof of concurrency: serialized, the first call waits out the timeout and fails."""
        self.in_flight += 1
        if self.in_flight >= 2:
            self.both_in_flight.set()
        await asyncio.wait_for(self.both_in_flight.wait(), timeout=5)
        return ToolResult.data("barrier")

    @tool("test.long", description="read-only; returns a lot of text", read_only=True)
    async def _long(self) -> ToolResult:
        return ToolResult.data("Население на България. " * 4000)

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


async def test_reasoning_that_eats_the_whole_allowance_retries_with_thinking_off(harness: Harness):
    """Running out of output room is not the same as having nothing to say.

    Measured on ardi 2026-09-06, "HTML презентация за българското население": two steps each
    spent the whole 16,384-token allowance on reasoning and wrote nothing, and the run died with
    "model returned an empty reply twice" — untrue — after 22 minutes and 382k tokens. The
    allowance is what ran out, so the next turn gets it for the answer instead of the thinking.
    """
    harness.chat.push(
        FakeTurn(text="", reasoning="thinking " * 400, completion_tokens=16_384, finish_reason="length"),
        FakeTurn(text="Ето презентацията."),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    # think=True explicitly, so the forced-off retry is visible rather than inferred.
    await harness.core.engine.create_run(text="направи ми презентация", conversation_id=conv.id, think=True)
    seen = await harness.wait_for(sub, "run.done", timeout=15)

    armed = [e for e in seen if e.type == "guard.armed"]
    assert [e.guard for e in armed] == ["reasoning_used_the_allowance"]
    assert "16,384" in armed[0].detail and "no answer" in armed[0].detail
    # The retry ran with thinking OFF, so the allowance went to the answer.
    calls = [e for e in seen if e.type == "model.call"]
    assert [c.think for c in calls] == [True, False]
    assert (await harness.core.store.list_messages(conv.id))[-1].content == "Ето презентацията."
    # ...and it is not mistaken for an empty reply on the way.
    assert not any(e.type == "guard.armed" and e.guard == "empty_reply" for e in seen)


async def test_an_answer_cut_off_at_the_allowance_is_continued_not_reported_as_finished(harness: Harness):
    """A truncated answer used to be persisted and the run reported `done`.

    Same conversation, the run that "succeeded": step 4 hit finish_reason=length after 16,384
    tokens and left a 14,274-character answer stopping mid-sentence. Nothing said so — it looked
    like a finished presentation.
    """
    harness.chat.push(
        FakeTurn(text="<html><body><h1>Част 1", completion_tokens=16_384, finish_reason="length"),
        FakeTurn(text="</h1><p>и краят.</p></body></html>"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="направи ми дълъг HTML", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=15)

    armed = [e for e in seen if e.type == "guard.armed"]
    assert [e.guard for e in armed] == ["answer_truncated"]
    # The partial text is kept and marked as partial, and the model was shown it to carry on from.
    msgs = await harness.core.store.list_messages(conv.id)
    partial = [m for m in msgs if m.partial]
    assert len(partial) == 1 and partial[0].content.endswith("Част 1")
    assert msgs[-1].content == "</h1><p>и краят.</p></body></html>" and not msgs[-1].partial
    second_call = harness.chat.calls[1][0]
    assert any("cut off" in m.content for m in second_call if m.name == "supervisor")
    assert any(m.content.endswith("Част 1") for m in second_call if m.role.value == "assistant")


async def test_empty_reply_is_nudged_once_then_fails(harness: Harness):
    harness.chat.push(FakeTurn(text=""), FakeTurn(text=""))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.failed")
    types = [e.type for e in seen]
    assert types.count("guard.armed") == 1 and types.count("guard.consumed") == 1
    # The message has to carry the evidence, not just the verdict: "empty reply" was what the
    # old one said about a model that had written 16,384 tokens of reasoning and been cut off.
    error = seen[-1].error
    assert "wrote nothing twice" in error
    assert "output tokens" in error and "reasoning" in error and "finish_reason" in error


async def test_a_message_sent_while_the_run_works_reaches_the_next_step(harness: Harness):
    """Steering: seeing it head the wrong way and being able to say so, without a restart.

    The message joins in Arsen's own voice, as an ordinary user message, so the model treats it
    like the one that started the run — and it is still there in the conversation afterwards.
    """
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.gate", arguments={})]),  # held open below
        FakeTurn(text="understood, doing it your way"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="do the thing", conversation_id=conv.id)

    # Wait until it is genuinely working — the first tool is in flight — then talk to it.
    async with asyncio.timeout(10):
        while "gate" not in tools.calls:
            await asyncio.sleep(0.01)
    assert harness.core.engine.steer(run.id, "  actually, do it the other way  ") is True
    tools.gate.set()  # let the tool finish; the run carries on rather than being cancelled

    seen = await harness.wait_for(sub, "run.done", timeout=15)
    steered = [e for e in seen if e.type == "run.steered"]
    assert steered and steered[0].text == "actually, do it the other way"

    # The model saw it, in Arsen's voice, on the step AFTER the one that was already running.
    second_call = harness.chat.calls[1][0]
    said = [m for m in second_call if m.role.value == "user" and not m.name]
    assert any(m.content == "actually, do it the other way" for m in said)
    # ...and it is part of the conversation from then on, not a one-off whisper.
    stored = await harness.core.store.list_messages(conv.id)
    mine = [m for m in stored if m.role.value == "user" and not m.name]
    assert [m.content for m in mine] == ["do the thing", "actually, do it the other way"]
    assert all(m.run_id == run.id for m in mine)


async def test_steering_a_run_that_has_already_finished_is_refused_not_swallowed(harness: Harness):
    """The caller turns a refusal into an ordinary new turn, so nothing Arsen typed is lost."""
    harness.chat.push(FakeTurn(text="done"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="quick one", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=10)

    assert harness.core.engine.steer(run.id, "too late") is False
    assert harness.core.engine.steer("run_never_existed", "hello") is False
    # An empty steer is refused too, rather than appending a blank turn.
    assert harness.core.engine.steer(run.id, "   ") is False


async def test_the_token_budget_counts_work_not_the_conversation_resent_each_step(harness: Harness):
    """Every step re-sends the whole conversation, so summing prompt_tokens counts the same text
    once per step. The prefix cache serves those repeats from KV — they are neither time nor
    compute. Measured on ardi 2026-09-06: a chat stopped at "224,594 tokens" of which 203,840
    were cached; 20k of actual work, judged against a 200k budget.
    """
    await with_tools(harness)
    # Six steps, each re-sending a 50k conversation of which 49k comes back from the cache.
    for i in range(6):
        harness.chat.push(
            FakeTurn(
                tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": str(i)})],
                prompt_tokens=50_000,
                cached_tokens=49_000,
                completion_tokens=100,
            )
        )
    harness.chat.push(FakeTurn(text="done", prompt_tokens=50_000, cached_tokens=49_000, completion_tokens=100))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    s = harness.core.settings.model_copy(deep=True)
    s.budgets[RunKind.CHAT].max_tokens = 200_000
    harness.core.apply_settings(s)
    run, _ = await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=15)

    done = await harness.core.store.get_run(run.id)
    assert done is not None
    # 7 x 50k = 350k counted the old way — well past the budget — but only 7 x 1.1k of it was work.
    assert done.usage.total_tokens > 300_000
    assert done.usage.processed_tokens < 10_000
    assert seen[-1].summary is None, f"stopped on a budget it never really used: {seen[-1].summary}"
    assert (await harness.core.store.list_messages(conv.id))[-1].content == "done"


async def test_the_token_budget_still_stops_a_run_that_is_really_working(harness: Harness):
    """The guard has to keep working: a loop that keeps adding NEW tokens still trips it."""
    await with_tools(harness)
    for i in range(30):
        harness.chat.push(
            FakeTurn(
                tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": str(i)})],
                prompt_tokens=60_000,
                cached_tokens=0,  # nothing reused: this is real reading, every step
                completion_tokens=500,
            )
        )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    s = harness.core.settings.model_copy(deep=True)
    s.budgets[RunKind.CHAT].max_tokens = 200_000
    harness.core.apply_settings(s)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=15)
    assert seen[-1].summary and "token budget" in seen[-1].summary
    assert "I stopped here" in (await harness.core.store.list_messages(conv.id))[-1].content


async def test_step_budget_ends_the_run_with_a_summary(harness: Harness):
    await with_tools(harness)
    # Always call a tool with different args → no repetition signal, only the budget.
    for i in range(4):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": str(i)})]))
    harness.chat.push(FakeTurn(text="Here are the four numbers I read: 0, 1, 2, 3."))
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
    # The run is over, but it hands over the work rather than only the verdict.
    assert "Here are the four numbers" in msgs[-1].content
    assert "[stopped: step budget reached (4 model calls)]" in msgs[-1].content


async def test_a_stopped_run_writes_its_answer_with_no_tools_and_no_thinking(harness: Harness):
    """19% of runs on 2026-09-07 ended as "I stopped here" and nothing else. Never again."""
    await with_tools(harness)
    for i in range(2):
        harness.chat.push(FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": str(i)})]))
    harness.chat.push(FakeTurn(text="Found two figures; the third source never answered.", reasoning="unused"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    s = harness.core.settings.model_copy(deep=True)
    s.budgets[RunKind.CHAT].max_steps = 2
    harness.core.apply_settings(s)
    await harness.core.engine.create_run(text="research", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=10)

    wrap_messages, wrap_tools = harness.chat.calls[-1]
    assert wrap_tools == [], "it must not be able to start new work"
    assert "You get no more tool calls" in wrap_messages[-1].content
    assert wrap_messages[-1].name == "supervisor"
    msgs = await harness.core.store.list_messages(conv.id)
    assert msgs[-1].content.startswith("Found two figures")


async def test_a_stopped_run_falls_back_to_the_old_note_if_the_closing_call_fails(harness: Harness):
    await with_tools(harness)
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c0", name="test.echo", arguments={"text": "x"})]))
    harness.chat.push(FakeTurn(fail_before_first_byte=True))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    s = harness.core.settings.model_copy(deep=True)
    s.budgets[RunKind.CHAT].max_steps = 1
    harness.core.apply_settings(s)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=10)
    msgs = await harness.core.store.list_messages(conv.id)
    assert "I stopped here" in msgs[-1].content, "a failed wrap-up still ends the run cleanly"


# --- B: thinking is a decision, not a default ------------------------------------------


def _think_flags(seen: list[object]) -> list[bool]:
    return [e.think for e in seen if e.type == "model.call"]  # type: ignore[attr-defined]


async def test_thinking_is_on_for_the_first_step_and_off_for_mechanical_ones(harness: Harness):
    await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"text": "a"})]),
        FakeTurn(tool_calls=[ToolCall(id="c2", name="test.echo", arguments={"text": "b"})]),
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="two reads", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    # Step 1 decides what to do; steps 2 and 3 are acting on a result that came back fine.
    assert _think_flags(seen) == [True, False, False]


async def test_an_error_a_steer_or_a_nudge_buys_the_next_step_its_reasoning(harness: Harness):
    await with_tools(harness)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"text": "fine"})]),
        FakeTurn(tool_calls=[ToolCall(id="c2", name="test.fail", arguments={})]),
        FakeTurn(text="that route is blocked; here is what I have"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    # first: yes · after a good result: no · after the failure: yes again
    assert _think_flags(seen) == [True, False, True]


async def test_adaptive_thinking_can_be_switched_off(harness: Harness):
    await with_tools(harness)
    harness.core.apply_settings(harness.core.settings.model_copy(update={"adaptive_thinking": False}))
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"text": "a"})]),
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert _think_flags(seen) == [True, True], "roles.chat.think decides every step again"


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


def _history_rewrites(calls: list) -> list[int]:
    """Which model calls changed a message the previous call had already sent.

    That is exactly what costs a prefix-cache miss: the server can only reuse the prompt up to
    the first byte that differs, so a rewrite anywhere means everything after it is prefilled
    again. Appending is free; editing is not.
    """
    broke: list[int] = []
    for n in range(1, len(calls)):
        before = [(m.role.value, m.content) for m in calls[n - 1][0]]
        now = [(m.role.value, m.content) for m in calls[n][0]]
        common = 0
        for a, b in zip(before, now, strict=False):
            if a != b:
                break
            common += 1
        if common < len(before):
            broke.append(n)
    return broke


async def test_compressing_the_context_does_not_fire_on_every_step(harness: Harness):
    """Trimming to just under the budget puts the next step straight back over it.

    Measured on ardi 2026-09-06: inside one run the rewrite fired again and again — 64 s, 59 s,
    49 s — each one re-reading ~78k tokens, because everything after a rewritten message has to
    be prefilled again. Coming back well under the line instead of just under it means the
    crossing happens rarely enough to pay for itself.
    """
    tools = await with_tools(harness)
    big = "row " * 1500  # ~6k chars per result

    async def big_echo(text: str = "") -> ToolResult:
        return ToolResult.data(big)

    tools._entries["test.echo"].fn = big_echo
    harness.enable(tool_context_token_budget=6_000)  # ~19k chars: three results fit, four do not
    harness.chat.push(
        *[FakeTurn(tool_calls=[ToolCall(id=f"c{i}", name="test.echo", arguments={"text": str(i)})]) for i in range(6)],
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="six big reads", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done", timeout=20)

    calls = harness.chat.calls
    assert len(calls) == 7
    rewrites = _history_rewrites(calls)
    # It must still do its job...
    assert rewrites, "the context was never compressed; the budget was not reached"
    truncated = [m for m in calls[-1][0] if m.role.value == "tool" and "[truncated" in m.content]
    assert truncated, "nothing was truncated by the end of the run"
    # ...but never twice in a row: a rewrite is followed by at least one step that only appends.
    assert not any(b + 1 in rewrites for b in rewrites), f"compressed on consecutive steps: {rewrites}"
    assert len(rewrites) <= len(calls) // 3, f"{len(rewrites)} rewrites in {len(calls)} calls: {rewrites}"
    # And the freshest result is always whole, whatever else was cut.
    newest = [m for m in calls[-1][0] if m.role.value == "tool"][-1]
    assert len(newest.content) > 5_000


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


# --- E: read-only calls in one batch go out together ------------------------------------


async def test_read_only_calls_in_one_batch_run_concurrently(harness: Harness):
    """test.barrier only returns once two of it are in flight, so this passes only in parallel."""
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="b1", name="test.barrier", arguments={}),
                ToolCall(id="b2", name="test.barrier", arguments={}),
            ]
        ),
        FakeTurn(text="both came back"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="two reads", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    results = [e for e in seen if e.type == "tool.result"]
    assert len(results) == 2
    assert all(r.result.kind is ToolResultKind.DATA for r in results), "serialized, the first would time out"
    assert tools.in_flight == 2


async def test_a_write_is_never_overtaken_by_a_read_that_followed_it(harness: Harness):
    """Grouping is by consecutive runs, so the model's own ordering across a mutation holds."""
    tools = await with_tools(harness)
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="r1", name="test.echo", arguments={"text": "before"}),
                ToolCall(id="w1", name="test.write", arguments={}),
                ToolCall(id="r2", name="test.echo", arguments={"text": "after"}),
            ]
        ),
        FakeTurn(text="ordered"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="mixed batch", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    assert tools.calls == ["before", "write", "after"]
    # Results reach the model in the order it asked for them, not the order they finished.
    msgs = await harness.core.store.list_messages(conv.id)
    assert [m.content for m in msgs if m.role.value == "tool"] == ["echo:before", "written", "echo:after"]


# --- G1: the same failing call is not tried a third time -------------------------------


async def test_a_call_that_failed_twice_the_same_way_is_refused_without_running(harness: Harness):
    tools = await with_tools(harness)
    call = ToolCall(id="f", name="test.fail", arguments={})
    for _ in range(3):
        harness.chat.push(FakeTurn(tool_calls=[call.model_copy(update={"id": f"f{_}"})]))
    harness.chat.push(FakeTurn(text="giving up on that route"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.core.apply_settings(harness.core.settings.model_copy(update={"repeated_call_threshold": 99}))
    await harness.core.engine.create_run(text="keep failing", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    texts = [e.result.text for e in seen if e.type == "tool.result"]
    assert texts[0] == "Error: boom" and texts[1] == "Error: boom"
    assert "will not be retried" in texts[2], texts[2]
    assert "boom" in texts[2], "the refusal quotes the error it keeps getting"
    # The third one never reached the tool.
    assert len(tools.calls) == 0, "test.fail records nothing, so count the tool.call events instead"
    assert sum(1 for e in seen if e.type == "tool.call") == 2


# --- A: research that was trimmed can be read back -------------------------------------


async def test_a_trimmed_tool_result_names_a_ref_that_reads_it_back(harness: Harness):
    """The 8.4% problem: a long result rides as a head, and the rest stays reachable."""
    await with_tools(harness)
    harness.core.apply_settings(harness.core.settings.model_copy(update={"tool_context_token_budget": 200}))
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="L1", name="test.long", arguments={})]),
        FakeTurn(tool_calls=[ToolCall(id="e1", name="test.echo", arguments={"text": "next"})]),
        FakeTurn(text="read the head"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="gather a lot", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")

    # By the third call the long result has been cut down, and the marker says how to get it back.
    third = harness.chat.calls[2][0]
    trimmed = next(m for m in third if m.role.value == "tool" and "[truncated" in m.content)
    assert 'jarvis.result_read(ref="L1")' in trimmed.content
    assert len(trimmed.content) < 2000, "the head, not the whole thing"

    # And the tool actually returns it.
    whole = await harness.core.store.tool_result("L1")
    assert whole is not None and len(whole[1]) > 80_000
    back = await harness.core.registry.call(
        "jarvis.result_read", {"ref": "L1", "offset": 0, "limit": 500}, cancel=asyncio.Event(), idempotency_key="k"
    )
    assert back.kind is ToolResultKind.DATA
    assert "Население на България" in back.text and "continue with offset=500" in back.text
    paged = await harness.core.registry.call(
        "jarvis.result_read", {"ref": "L1", "offset": 500, "limit": 100}, cancel=asyncio.Event(), idempotency_key="k2"
    )
    assert "500-600 of" in paged.text
    missing = await harness.core.registry.call(
        "jarvis.result_read", {"ref": "nope"}, cancel=asyncio.Event(), idempotency_key="k3"
    )
    assert missing.kind is ToolResultKind.ERROR and "no tool result" in missing.text
