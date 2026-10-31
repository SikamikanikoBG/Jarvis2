"""Alpha pass 2 findings (07-alpha/FINDINGS.md, "Pass 2", 2026-10-30), each as a test."""

from __future__ import annotations

from typing import Any

from jarvis_core.models.fake import FakeTurn
from jarvis_proto import Channel, RunBudget, RunKind, TasksSettings, TaskState, ToolCall
from tests.conftest import Harness
from tests.test_loop import with_tools
from tests.test_tasks import FakeHost, ask, delegate, echo, finish, pushes, reported, tasks_on, the_task, until

TABLE = "| River | Length |\n|---|---|\n| Danube | 2,850 km |\n| Iskar | 368 km |\n| Maritsa | 480 km |"


async def _report(h: Harness, conv_id: str) -> str:
    return (await h.core.store.list_messages(conv_id))[-1].content


# --- N-1: the report is the result ---------------------------------------------------------------------


async def test_the_report_carries_the_result_itself_not_a_description_of_it(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("a table of rivers",))]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]), summary="I compiled a table of the rivers.")]),
        FakeTurn(text=f"Here are the rivers:\n\n{TABLE}"),
    )
    _, conv = await ask(harness, "a table of Bulgarian rivers")
    await reported(harness)
    report = await _report(harness, conv.id)
    assert TABLE in report  # the deliverable, as written
    assert "I compiled a table" not in report  # not the task's description of its work


async def test_a_result_written_beside_task_finish_reaches_the_report_when_the_last_reply_points_at_it(
    harness: Harness,
):
    """2026-10-31, the eight-meeting plan on the live core: the table came with the first task.finish,
    a re-finish fixed an evidence ref, and the final reply said "the full proposal is in my message
    above". The report carried only that last line, so Arsen never saw the table."""
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("a table of rivers",))]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(text=f"Here are the rivers:\n\n{TABLE}", tool_calls=[finish("f1", (1, True, ["nope"]))]),
        FakeTurn(text="The ref was wrong; recording it again.", tool_calls=[finish("f2", (1, True, ["c1"]))]),
        FakeTurn(text="All done - the full table is in my message above."),
    )
    _, conv = await ask(harness, "a table of Bulgarian rivers")
    await reported(harness)
    report = await _report(harness, conv.id)
    assert TABLE in report and "in my message above" in report
    assert report.index(TABLE) < report.index("in my message above")  # "above" is true in the report too
    assert "recording it again" not in report  # the evidence fix is not the result


async def test_a_result_too_long_for_a_message_is_cut_at_a_paragraph_and_attached_whole(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    long = "\n\n".join(f"Section {i}: " + "x" * 600 for i in range(20))  # few tokens, many characters
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]), FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]), FakeTurn(text=long)
    )
    _, conv = await ask(harness, "a long write-up")
    task = await reported(harness)
    report = await _report(harness, conv.id)
    assert report.count("Section ") < 20 and "Section 0:" in report
    assert "(The whole result is attached: Hotels.md.)" in report and "task's own log" not in report

    async def attached() -> list[Any]:
        return await harness.core.attachments.for_message(task.report_message_id or "")

    await until(attached)
    files = await attached()
    assert [f.name for f in files] == ["Hotels.md"] and "Section 19:" in (files[0].text or "")


async def test_a_steer_after_the_report_reports_the_new_result(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("the rivers",))]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]), summary="Rivers listed.")]),
        FakeTurn(text=TABLE),
    )
    _, conv = await ask(harness, "rivers")
    task = await reported(harness)
    split.task.push(FakeTurn(text="The Rila monastery became a UNESCO site in 1983."))
    await harness.core.tasks.steer(task.id, "and when did Rila become a UNESCO site?")
    task = await reported(
        harness, lambda t: t.report_message_id is not None and t.report_message_id != task.report_message_id
    )
    report = await _report(harness, conv.id)
    assert "1983" in report and TABLE not in report
    assert task.state is TaskState.DONE  # what was verified before stays verified


# --- F-5: the stop reason once ------------------------------------------------------------------------


async def test_a_task_stopped_with_nothing_written_says_why_once(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    harness.enable(budgets={**harness.core.settings.budgets, RunKind.TASK: RunBudget(max_steps=1)})
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[echo("c1")]), FakeTurn(text=""))  # the wrap-up writes nothing
    _, conv = await ask(harness, "research")
    await reported(harness)
    report = await _report(harness, conv.id)
    assert report.count("step budget reached") == 1


# --- N-4: a denied action is not asked again ------------------------------------------------------------


async def test_after_a_deny_the_same_call_is_refused_not_asked_again(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    write = ToolCall(id="w1", name="test.send", arguments={})
    split.task.push(
        FakeTurn(tool_calls=[write]),
        FakeTurn(tool_calls=[ToolCall(id="w2", name="test.send", arguments={})]),  # the same call again
        FakeTurn(tool_calls=[finish("f1", (1, False, []))]),
        FakeTurn(text="He declined; not done."),
    )
    await ask(harness, "write it")
    task = await the_task(harness, lambda t: t.state is TaskState.WAITING)
    assert task.waiting is not None
    await harness.core.engine.confirm(task.waiting.run_id, task.waiting.call_id, False, "no")
    task = await reported(harness)
    assert tools.calls == []
    events = await harness.core.store.list_events(task.run_id or "")
    assert [e["call_id"] for e in events if e["type"] == "tool.confirm_requested"] == ["w1"]  # asked once
    refused = next(m for m in await harness.core.store.list_run_messages(task.run_id or "") if m.tool_call_id == "w2")
    assert "already turned down exactly this test.send call" in refused.content


# --- N-3: one failed call from a machine the brief does not need is a normal error ------------------------


async def test_a_failed_call_to_a_machine_the_brief_does_not_need_does_not_end_the_task(harness: Harness):
    split = await tasks_on(harness)
    host = FakeHost()
    harness.core.registry.add(host)
    await harness.core.registry.refresh()
    await with_tools(harness)
    pushes(harness)
    host.drop_on_read = lambda: harness.core.registry.mark_unavailable(host, "suspended")
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))  # needs nothing named
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="h1", name="hosty.read", arguments={})]),
        FakeTurn(tool_calls=[echo("c2")]),  # it works around it
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c2"]))]),
        FakeTurn(text="Done another way."),
    )
    await ask(harness, "read it")
    task = await reported(harness)
    assert task.state is TaskState.DONE


# --- N-5: an outward item done by a tool he freed from approval can verify ----------------------------------


async def test_an_outward_item_done_by_a_tool_freed_from_approval_verifies(harness: Harness):
    split = await tasks_on(harness, no_approval=["test.send"])
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("it is written",), outward=(1,))]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="w1", name="test.send", arguments={})]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["w1"]))]),
        FakeTurn(text="Written."),
    )
    await ask(harness, "write it")
    task = await reported(harness)
    assert tools.calls == ["send"] and task.state is TaskState.DONE


# --- N-2: a spoken answer from memory is not learned as a fact ------------------------------------------------


async def test_a_voice_answer_without_a_tool_result_is_not_a_source_for_the_knowledge_graph(harness: Harness):
    harness.enable(kg_learning=True, tasks=TasksSettings(enabled=False))
    learned: list[tuple[str, str]] = []
    harness.core.learner.schedule = lambda conv, user, answer, mid: learned.append((user, answer))  # type: ignore[method-assign]
    harness.chat.push(FakeTurn(text="Musala is 2,925 metres - the highest in the Balkans."))
    _, conv = await harness.core.engine.create_run(
        text="how high is Musala, roughly?", channel=Channel.VOICE, from_owner=True
    )
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    assert learned == [("how high is Musala, roughly?", "")]
    # A typed answer, or a spoken one with a tool result behind it, still teaches as before.
    harness.chat.push(FakeTurn(text="Musala is 2,925 m."))
    await harness.core.engine.create_run(text="how high is Musala, roughly?", conversation_id=conv.id, from_owner=True)
    await harness.wait_for(sub, "run.done")
    assert learned[-1][1] == "Musala is 2,925 m."


async def test_a_noted_task_is_told_his_words_are_the_brief_and_jarvis_lines_are_unchecked(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]), FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]), FakeTurn(text=".")
    )
    await ask(harness, "x")
    await reported(harness)
    system = split.task.calls[0][0][0].content
    assert "What Jarvis said back to him there was from memory and is not checked" in system
