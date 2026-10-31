"""Alpha pass 1 findings (idea-to-uat/delegated-tasks/07-alpha/FINDINGS.md, 2026-10-30): each one
reproduced as a test, so the fix stays fixed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.loop import HAND_OFF_NOTE, HANDED_OFF_NOTE
from jarvis_core.features.web import WebTools
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import Role, RoleName, RunBudget, RunKind, RunStatus, Settings, TasksSettings, TaskState, ToolCall
from tests.conftest import Harness
from tests.test_loop import with_tools
from tests.test_tasks import ask, delegate, echo, finish, pushes, reported, tasks_on, the_task, until

LONG_ASK = "Find me the three best-rated hotels near the Rathaus in Vienna for 14-16 November and compare them."


def title_only(call_id: str = "d1", title: str = "Hotels near the Rathaus") -> ToolCall:
    return ToolCall(id=call_id, name="tasks.delegate", arguments={"title": title})


async def _done(h: Harness, conv_id: str) -> None:
    async def finished() -> bool:
        runs = await h.core.store.list_runs(conv_id)
        return bool(runs) and all(r.status.terminal for r in runs)

    await until(finished)


# --- F-1 / F-2 / F-8: delegation is a shape the turn can take, and the turn ends on the ack ---------


async def test_the_preflight_offers_delegation_and_the_turn_hands_off_without_a_plan(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    harness.enable(planning_enabled=True)
    split.push(
        FakeTurn(text='{"tier": "delegate"}'),  # the pre-flight's judgement
        FakeTurn(tool_calls=[title_only()]),
        # Asked for the ack, the model reaches for a tool anyway: it is not run.
        FakeTurn(text="On it - I'll come back with three hotels.", tool_calls=[echo("x1")]),
    )
    run, conv = await ask(harness, LONG_ASK)
    await _done(harness, conv.id)
    assert tools.calls == []  # the turn did no work itself
    assert (await harness.core.store.get_run(run.id)).plan is None  # no plan to do it, or to wait for it
    preflight = split.calls[0][0][-1].content
    assert '"delegate"' in preflight and "background task" in preflight
    first, second = split.calls[1][0], split.calls[2][0]
    assert first[-1].content == HAND_OFF_NOTE and second[-1].content == HANDED_OFF_NOTE
    events = await harness.core.store.list_events(run.id)
    assert next(e["think"] for e in events if e["type"] == "model.call") is False  # no reasoning for a hand-off
    last = (await harness.core.store.list_run_messages(run.id))[-1]
    assert last.role is Role.ASSISTANT and last.content.startswith("On it")
    assert len(await harness.core.tasks.store.list()) == 1


async def test_without_tasks_the_preflight_does_not_offer_delegation(harness: Harness):
    harness.enable(planning_enabled=True)
    harness.chat.push(FakeTurn(text='{"tier": "simple"}'), FakeTurn(text="Here."))
    _, conv = await ask(harness, LONG_ASK)
    await _done(harness, conv.id)
    assert '"delegate"' not in harness.chat.calls[0][0][-1].content


async def test_a_plan_made_before_the_hand_off_is_dropped_not_waited_on(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    harness.enable(planning_enabled=True)
    split.push(
        FakeTurn(text='{"tier": "multi_step"}'),
        FakeTurn(text='{"goal": "hotels", "steps": ["search", "compare", "wait for the task"]}'),
        FakeTurn(tool_calls=[title_only()]),
        FakeTurn(text="Taken - the hotels come back here."),
    )
    run, conv = await ask(harness, LONG_ASK)
    await _done(harness, conv.id)
    done = await harness.core.store.get_run(run.id)
    assert done.status is RunStatus.DONE and done.plan is not None
    assert {s.status.value for s in done.plan.steps} == {"skipped"}
    # No "the plan still has open steps" nudge after the hand-off: the ack was the last step.
    assert not any(m.name == "supervisor" for m in await harness.core.store.list_run_messages(run.id))


async def test_a_title_is_enough_and_the_task_writes_its_brief_from_the_chat(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(text="Vienna it is - for which dates?"))
    _, conv = await ask(harness, "I'm going to Vienna next month")
    await _done(harness, conv.id)
    long_title = "Hotels near the Rathaus in Vienna for the fourteenth to sixteenth of November, two adults, under 180"
    split.push(FakeTurn(tool_calls=[title_only(title=long_title)]), FakeTurn(text="On it."))
    split.task.push(
        FakeTurn(
            tool_calls=[
                ToolCall(
                    id="b1",
                    name="task.brief",
                    arguments={"goal": "g", "context": "c", "dod": [{"item": "three hotels"}]},
                )
            ]
        ),
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["[ref c1]"]))]),
        FakeTurn(text="Three hotels."),
    )
    await ask(harness, "find hotels near the Rathaus, 14-16 Nov", conv.id)
    task = await reported(harness)
    assert len(task.title) <= 80 and task.title.startswith("Hotels near the Rathaus")  # shortened, not refused
    opening = next(m.content for m in split.task.calls[0][0] if m.role is Role.USER and m.name is None)
    assert "Its brief is not written yet" in opening
    assert "Arsen: I'm going to Vienna next month" in opening and "Jarvis: Vienna it is - for which dates?" in opening
    assert "Arsen: find hotels near the Rathaus, 14-16 Nov" in opening
    # F-4: the result header carries the ref, and "[ref c1]" as copied is the ref c1.
    results = [m for m in await harness.core.store.list_messages(task.conversation_id or "") if m.role is Role.TOOL]
    assert next(m for m in results if m.tool_call_id == "c1").content.startswith("[ref c1] ")
    assert task.state is TaskState.DONE and task.outcome is not None and task.outcome.dod[0].verified


# --- F-9 / F-14: the same ask twice, and what the ack may say ------------------------------------------------


async def test_the_same_words_again_while_the_task_is_open_make_no_second_task(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate("d1")]), FakeTurn(text="On it."))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]))
    _, conv = await ask(harness, LONG_ASK)
    await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    split.push(FakeTurn(tool_calls=[delegate("d2")]), FakeTurn(text="Already on it."))
    run2, _ = await ask(harness, LONG_ASK, conv.id)
    await until(lambda: _terminal(harness, run2.id))
    assert len(await harness.core.tasks.store.list()) == 1
    result = next(m for m in await harness.core.store.list_run_messages(run2.id) if m.role is Role.TOOL)
    assert result.content.startswith("Already on it:")
    tools.gate.set()


async def _terminal(h: Harness, run_id: str) -> bool:
    run = await h.core.store.get_run(run_id)
    return run is not None and run.status.terminal


async def test_the_ack_says_queued_when_it_is_queued_and_never_done(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate("d1", title="First")]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]))
    _, conv = await ask(harness, "first long job, please take it")
    await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    split.push(FakeTurn(tool_calls=[delegate("d2", title="Second")]), FakeTurn(text="ok"))
    run2, _ = await ask(harness, "second long job, please take it too", conv.id)
    await until(lambda: _terminal(harness, run2.id))
    said = next(m for m in await harness.core.store.list_run_messages(run2.id) if m.role is Role.TOOL).content
    assert "It is queued behind 1 other task(s)" in said and "It is not done yet" in said
    assert "task_" not in said  # no internal ids for him to read out (L-8)
    second = await the_task(harness, lambda t: t.title == "Second")
    assert second.state is TaskState.QUEUED and second.started_at is None  # queued is not started (L-4)
    tools.gate.set()


# --- F-5: a task the budget stops still ends with its outcome ----------------------------------------------


async def test_a_task_stopped_by_its_budget_ends_with_task_finish_and_a_clean_report(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    harness.enable(budgets={**harness.core.settings.budgets, RunKind.TASK: RunBudget(max_steps=2)})
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    summary = "Three hotels: " + "a long, useful comparison. " * 60
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[echo("c2", "y")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]), summary=summary)]),  # the wrap-up
    )
    _, conv = await ask(harness, "big research")
    task = await reported(harness)
    assert task.state is TaskState.DONE and task.outcome is not None and task.outcome.dod[0].verified
    report = (await harness.core.store.list_messages(conv.id))[-1].content
    assert summary.strip() in report  # whole, not cut at 1,200 characters
    assert "[supervisor]" not in report and "[stopped:" not in report
    wrap = split.task.calls[-1]
    assert [t.name for t in wrap[1]] == ["task.finish"]


# --- F-6: a restart is an interruption, not a cancel ------------------------------------------------------


async def test_a_restart_in_the_middle_of_a_model_step_resumes_the_task_and_does_not_cancel_it(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(text="thinking " * 400, token_delay_s=0.02))
    await ask(harness, "research")
    task = await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: len(split.task.calls) == 1)
    await asyncio.sleep(0.2)  # well inside the streaming answer
    await harness.restart()
    run = await harness.core.store.get_run(task.run_id or "")
    assert run is not None and run.status is not RunStatus.CANCELLED
    await tasks_on(harness)
    await harness.core.tasks.reconcile()
    task = await harness.core.tasks.store.get(task.id)
    assert task is not None and task.state is not TaskState.CANCELLED


# --- F-7: a task let go by an approval does not run beside the one that took its slot ------------------------


async def test_an_approved_task_waits_for_the_running_one_before_its_next_step(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate("d1", title="A"), delegate("d2", title="B")]), FakeTurn(text="two"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="w1", name="test.send", arguments={})]),  # A asks
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]),  # B takes the slot
        FakeTurn(tool_calls=[finish("fb", (1, True, ["g1"]))]),
        FakeTurn(text="B done"),
        FakeTurn(tool_calls=[finish("fa", (1, True, ["w1"]))]),
        FakeTurn(text="A done"),
    )
    await ask(harness, "two jobs")
    a = await the_task(harness, lambda t: t.title == "A" and t.state is TaskState.WAITING)
    await until(lambda: "gate" in tools.calls)
    assert a.waiting is not None
    await harness.core.engine.confirm(a.waiting.run_id, a.waiting.call_id, True)
    await until(lambda: "send" in tools.calls)
    await asyncio.sleep(0.3)
    assert len(split.task.calls) == 2  # A holds before its next model step while B works
    tools.gate.set()
    await until(lambda: _both_done(harness))


async def _both_done(h: Harness) -> bool:
    tasks = await h.core.tasks.store.list()
    return len(tasks) == 2 and all(t.state is TaskState.DONE for t in tasks)


# --- F-10: a search backend that does not answer is down, said as such -------------------------------------


async def test_search_engines_that_do_not_answer_mark_the_search_down(harness: Harness):
    payload = {"results": [], "unresponsive_engines": [["brave", "too many requests"], ["duckduckgo", "CAPTCHA"]]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    harness.core.settings.searxng_url = "http://searx"
    web = WebTools(lambda: harness.core.settings, client=client)
    result = await web.call("web.search", {"query": "x"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    assert result.error is not None and "brave too many requests" in result.error
    assert web.error is not None
    ok = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"results": [{"url": "https://a.b", "title": "t"}]})
        )
    )
    web._client = ok  # pyright: ignore[reportPrivateUsage]
    await web.call("web.search", {"query": "x"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    assert web.error is None
    harness.core.settings.searxng_url = None
    assert not web.configured  # not set up: web.search is not offered at all


# --- F-12: words for a task that ends before it reads them ---------------------------------------------------


async def test_words_a_task_never_read_carry_it_on_instead_of_being_lost(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]), FakeTurn(text="Here it is.")
    )
    await ask(harness, "research")
    task = await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: "gate" in tools.calls)
    real = harness.core.engine.steer
    harness.core.engine.steer = lambda run_id, text, by_owner=True: True  # type: ignore[method-assign]  # taken, never read: the race
    await harness.core.tasks.steer(task.id, "3 nights, not 2")
    harness.core.engine.steer = real  # type: ignore[method-assign]
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]),
        FakeTurn(text="3 nights."),
    )
    tools.gate.set()
    task = await reported(harness)
    words = [
        m.content
        for m in await harness.core.store.list_messages(task.conversation_id or "")
        if m.role is Role.USER and m.name is None
    ]
    assert words[-1] == "3 nights, not 2" and task.state is TaskState.DONE


# --- F-13: switching tasks off mid-flight, and a partial PATCH -------------------------------------------------


async def test_switching_tasks_off_lets_a_working_task_finish_properly(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["g1"]))]),
        FakeTurn(text="done"),
    )
    await ask(harness, "research")
    await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: "gate" in tools.calls)
    harness.enable(tasks=TasksSettings(enabled=False))
    tools.gate.set()
    task = await reported(harness)
    assert task.state is TaskState.DONE


def test_a_partial_patch_of_a_settings_section_keeps_the_rest(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="t"))
    core.adapters.fakes = {r: FakeAdapter() for r in RoleName}
    h = {"Authorization": "Bearer t"}
    with TestClient(create_app(core.config, core=core)) as client:
        client.patch("/api/settings", json={"tasks": {"enabled": True}, "mcp_servers": []}, headers=h)
        after = client.patch("/api/settings", json={"tasks": {"max_running": 2}}, headers=h).json()
        assert after["tasks"]["enabled"] is True and after["tasks"]["max_running"] == 2
        # A dict keyed by name is still replaced whole: an account rule can be removed.
        client.patch("/api/settings", json={"triage": {"account_rules": {"a@b.c": {}}}}, headers=h)
        cleared = client.patch("/api/settings", json={"triage": {"account_rules": {}}}, headers=h).json()
        assert cleared["triage"]["account_rules"] == {}
        assert Settings.model_validate(cleared).tasks.enabled is True


# --- F-16: a cancelled task's report shows what it got to -------------------------------------------------------


async def test_a_cancelled_task_reports_the_work_it_got_to(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(
            text="Found two hotels so far: Rathauspark and Am Konzerthaus.",
            tool_calls=[ToolCall(id="s1", name="test.slow", arguments={})],
        ),
        # The cancel's wrap-up: what it got to, from the conversation (alpha pass 2, F-16).
        FakeTurn(text="So far: Rathauspark and Am Konzerthaus, both under 180; the third was not checked."),
    )
    _, conv = await ask(harness, "research")
    task = await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: len(split.task.calls) == 1)
    await asyncio.sleep(0.1)
    await harness.core.tasks.cancel(task.id)
    task = await reported(harness)
    report = (await harness.core.store.list_messages(conv.id))[-1].content
    assert task.state is TaskState.CANCELLED and "So far: Rathauspark and Am Konzerthaus" in report
    wrap = split.task.calls[-1][0][-1]
    assert wrap.name == "supervisor" and "cancelled by Arsen" in wrap.content
    assert task.outcome is not None and [d.item for d in task.outcome.dod] == ["three options"]  # items kept
