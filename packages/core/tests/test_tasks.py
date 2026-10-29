"""Delegated tasks (docs/stories/13_delegated_tasks.md): a brief and a DoD, a run of its own,
a card and an honest report in the chat that asked, and nothing outward without Arsen."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.features.tasks import HealthBlock
from jarvis_core.models.base import ModelEvent, reset_endpoint_semaphores
from jarvis_core.models.factory import current_run_kind
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import (
    Confirmations,
    ConversationKind,
    Message,
    Role,
    RoleName,
    Run,
    RunBudget,
    RunKind,
    RunStatus,
    Task,
    TasksSettings,
    TaskState,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from tests.conftest import Harness
from tests.test_loop import with_tools


class SplitFake(FakeAdapter):
    """The chat's turns and the tasks' turns in two scripts. The asking chat and the task's run
    share the engine and run side by side; one script would hand one of them the other's turn."""

    def __init__(self) -> None:
        super().__init__()
        self.task = FakeAdapter()

    def stream(
        self, messages: list[Message], tools: list[ToolSpec], *, cancel: asyncio.Event
    ) -> AsyncIterator[ModelEvent]:
        if current_run_kind.get() is RunKind.TASK:
            return self.task.stream(messages, tools, cancel=cancel)
        return super().stream(messages, tools, cancel=cancel)


async def tasks_on(h: Harness, **overrides: Any) -> SplitFake:
    split = h.chat if isinstance(h.chat, SplitFake) else SplitFake()
    h.chat = split
    h.core.adapters.fakes = {r: split for r in RoleName}
    h.core.adapters.fakes[RoleName.JUDGE] = h.judge
    h.enable(tasks=TasksSettings(enabled=True, **overrides))
    await h.core.tasks.start()
    return split


def pushes(h: Harness) -> list[dict[str, Any]]:
    """Every notice the tasks sent, instead of a real push."""
    sent: list[dict[str, Any]] = []

    async def push(payload: dict[str, Any], *, topic: str | None = None) -> int:
        sent.append(payload)
        return 1

    h.core.notify.push = push  # type: ignore[method-assign]
    return sent


def delegate(
    call_id: str = "d1",
    *,
    title: str = "Hotels",
    dod: tuple[str, ...] = ("three options",),
    outward: tuple[int, ...] = (),
    needs: tuple[str, ...] = (),
) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="tasks.delegate",
        arguments={
            "title": title,
            "goal": f"{title}: the goal",
            "context": "Arsen + 1, 14-16 Nov",
            "dod": [{"item": item, "outward": i in outward} for i, item in enumerate(dod, 1)],
            "needs": list(needs),
            "assumptions": ["two adults"],
        },
    )


def finish(call_id: str, *items: tuple[int, bool, list[str]], summary: str = "Found them.") -> ToolCall:
    return ToolCall(
        id=call_id,
        name="task.finish",
        arguments={"summary": summary, "dod": [{"index": i, "met": m, "refs": refs} for i, m, refs in items]},
    )


def echo(call_id: str, text: str = "x") -> ToolCall:
    return ToolCall(id=call_id, name="test.echo", arguments={"text": text})


async def until(cond: Callable[[], Any], timeout: float = 8.0) -> None:
    async with asyncio.timeout(timeout):
        while not await _value(cond()):
            await asyncio.sleep(0.02)


async def _value(v: Any) -> Any:
    return await v if asyncio.iscoroutine(v) else v


async def the_task(h: Harness, pred: Callable[[Task], bool] = lambda t: True, timeout: float = 8.0) -> Task:
    found: list[Task] = []

    async def check() -> bool:
        found[:] = [t for t in await h.core.tasks.store.list() if pred(t)]
        return bool(found)

    await until(check, timeout)
    return found[0]


async def ask(h: Harness, text: str, conversation_id: str | None = None, **kw: Any):
    return await h.core.engine.create_run(text=text, conversation_id=conversation_id, from_owner=True, **kw)


def offered(adapter: FakeAdapter, call: int = 0) -> set[str]:
    return {t.name for t in adapter.calls[call][1]}


def drain(sub: Any) -> list[Any]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


# --- delegate → card → verified report ---------------------------------------------------------


async def test_a_delegated_task_runs_in_its_own_conversation_and_reports_back_verified(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    sent = pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="On it - I'll come back with three hotels."))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1", "vienna")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]),
        FakeTurn(text="Three hotels, all under 180."),
    )
    elsewhere = await harness.core.store.create_conversation(title="Another chat")
    watcher = harness.subscribe(elsewhere.id)
    run, conv = await ask(harness, "Find me a hotel near the Rathaus for 14-16 Nov")

    task = await the_task(harness, lambda t: t.state.terminal and t.report_message_id is not None)
    assert task.state is TaskState.DONE
    assert task.outcome is not None and task.outcome.dod[0].verified
    # His own words, verbatim, copied by the code - not the model's paraphrase.
    assert task.brief.asked == ["Find me a hotel near the Rathaus for 14-16 Nov"]
    assert task.brief.assumptions == ["two adults"]
    # Its own conversation, hidden behind the card: kind task, filed under the chat that asked.
    own = await harness.core.store.get_conversation(task.conversation_id or "")
    assert own is not None and own.kind is ConversationKind.TASK and own.folder_key == conv.id
    assert not own.unread  # never unread: the report in the chat that asked is what he reads
    # The chat that asked was free at once: its run ended on its one-line answer.
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.DONE
    # The report, after the core's note, in the chat that asked.
    msgs = await harness.core.store.list_messages(conv.id)
    note, report = msgs[-2], msgs[-1]
    assert note.role is Role.USER and note.name == "task" and note.task_id == task.id
    assert note.content.startswith("[core note, not from Arsen]")
    assert report.role is Role.ASSISTANT and report.task_id == task.id and report.run_id is None
    assert "Task: Hotels — done" in report.content and "✅ three options" in report.content
    # Global: a client looking at another chat hears the task move (the header chip).
    assert any(getattr(e, "type", "") == "task.changed" for e in drain(watcher))
    # One quiet notice that opens the card - not a ring, not Discord.
    assert [p["type"] for p in sent] == ["notice"]
    assert sent[0]["path"] == f"/c/{conv.id}?task={task.id}" and "done" in sent[0]["title"]
    # The task's run was offered its own tools and not the chat's.
    tools = offered(split.task)
    assert {"task.finish", "task.ask"} <= tools and not any(n.startswith("tasks.") for n in tools)
    assert "sessions.say" not in tools and "jarvis.call_me" not in tools
    assert "tasks.delegate" in offered(split) and "task.finish" not in offered(split)


async def test_a_forged_or_failed_ref_is_claimed_not_verified_and_never_done(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("a price list", "a booking link"))]), FakeTurn(text="Taking it."))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="c2", name="test.fail", arguments={})]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c999"]), (2, True, ["c2"]))]),
        FakeTurn(text="All done!"),
    )
    _, conv = await ask(harness, "prices and a link")
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.PARTIAL  # claimed is not shown
    first, second = task.outcome.dod if task.outcome else []
    assert first.met and not first.verified and "c999 is not a call or a file of this task" in (first.why or "")
    assert second.met and not second.verified and "failed" in (second.why or "")
    report = (await harness.core.store.list_messages(conv.id))[-1].content
    assert report.count("claimed, not verified") == 2 and "— done**" not in report


async def test_bookkeeping_is_not_evidence_and_an_outward_item_needs_an_approved_action(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(
        FakeTurn(tool_calls=[delegate(dod=("the mail is sent", "the list is read"), outward=(1,))]),
        FakeTurn(text="ok"),
    )
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="w1", name="test.write", arguments={})]),
        FakeTurn(tool_calls=[echo("r1")]),
        # Item 1 cites the approved write; item 2 cites the task's own finish call.
        FakeTurn(tool_calls=[finish("f1", (1, True, ["w1"]), (2, True, ["f1"]))]),
        FakeTurn(text="Sent."),
    )
    await ask(harness, "send it")
    task = await the_task(harness, lambda t: t.state is TaskState.WAITING)
    assert tools.calls == []
    assert task.waiting is not None
    await harness.core.engine.confirm(task.waiting.run_id, task.waiting.call_id, True)
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    sent, read = task.outcome.dod if task.outcome else []
    assert sent.outward and sent.verified
    assert not read.verified and "bookkeeping" in (read.why or "")
    assert task.state is TaskState.PARTIAL


async def test_an_outward_item_cited_by_a_read_is_not_verified(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("the mail is sent",), outward=(1,))]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("r1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["r1"]))]),
        FakeTurn(text="Sent."),
    )
    await ask(harness, "send it")
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.PARTIAL
    assert task.outcome is not None and "only read" in (task.outcome.dod[0].why or "")


# --- nothing outward without him ----------------------------------------------------------------


class _TypeArgs(BaseModel):
    text: str
    submit: bool = False


class FakeBrowser(BuiltinProvider):
    """browser.type the way the extension declares it: not read-only, not destructive."""

    name = "fakebrowser"

    def __init__(self) -> None:
        super().__init__()
        self.typed: list[dict[str, Any]] = []

    @tool("browser.type", description="type into the page", args=_TypeArgs)
    async def _type(self, text: str, submit: bool = False) -> ToolResult:
        self.typed.append({"text": text, "submit": submit})
        return ToolResult.data("typed")


async def test_browser_type_with_submit_in_a_task_waits_for_approval_even_with_confirmations_off(harness: Harness):
    split = await tasks_on(harness)
    browser = FakeBrowser()
    harness.core.registry.add(browser)
    await harness.core.registry.refresh()
    sent = pushes(harness)
    # Live-shaped: nothing asks in a chat, and he has even freed the browser by name.
    harness.enable(confirmations=Confirmations(mode="off", always_allow=["browser.*"], outbound_check=[]))
    split.push(FakeTurn(tool_calls=[delegate(title="Post the review")]), FakeTurn(text="ok"))
    typed = ToolCall(id="t1", name="browser.type", arguments={"text": "Five stars", "submit": True})
    split.task.push(
        FakeTurn(tool_calls=[typed]), FakeTurn(tool_calls=[finish("f1", (1, False, []))]), FakeTurn(text=".")
    )
    await ask(harness, "post my review")
    task = await the_task(harness, lambda t: t.state is TaskState.WAITING)
    assert browser.typed == []
    assert task.waiting is not None and task.waiting.name == "browser.type"
    assert task.waiting.arguments == {"text": "Five stars", "submit": True}  # the card shows exactly this
    assert (await harness.core.store.get_run(task.waiting.run_id)).status is RunStatus.WAITING_USER
    await until(lambda: any("approval" in p["title"] for p in sent))
    assert sent[-1]["type"] == "notice" and sent[-1]["path"].endswith(f"?task={task.id}")  # opens the card
    await harness.core.engine.confirm(task.waiting.run_id, task.waiting.call_id, True)
    await the_task(harness, lambda t: t.state.terminal)
    assert browser.typed == [{"text": "Five stars", "submit": True}]


async def test_the_same_browser_type_in_a_chat_is_unchanged_by_tasks(harness: Harness):
    """Regression: the task rule must not leak into an ordinary chat (mode=off asks nothing)."""
    split = await tasks_on(harness)
    browser = FakeBrowser()
    harness.core.registry.add(browser)
    await harness.core.registry.refresh()
    harness.enable(confirmations=Confirmations(mode="off", outbound_check=[]))
    split.push(
        FakeTurn(tool_calls=[ToolCall(id="t1", name="browser.type", arguments={"text": "hi"})]), FakeTurn(text=".")
    )
    _, conv = await ask(harness, "type hi")
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    assert browser.typed == [{"text": "hi", "submit": False}]


class _HostArgs(BaseModel):
    command: str = ""


class FakeHost(BuiltinProvider):
    """A machine with a shell, file writes and a read - and a connection that can drop."""

    name = "hosty"

    def __init__(self) -> None:
        super().__init__()
        self.ran: list[str] = []
        self.drop_on_read: Callable[[], None] | None = None

    @tool("hosty.shell_run", description="run a command", args=_HostArgs, destructive=True)
    async def _shell(self, command: str = "") -> ToolResult:
        self.ran.append(command)
        return ToolResult.data("ran")

    @tool("hosty.fs_write", description="write a file", args=_HostArgs, destructive=True)
    async def _write(self, command: str = "") -> ToolResult:
        self.ran.append("write")
        return ToolResult.data("written")

    @tool("hosty.read", description="read something", args=_HostArgs, read_only=True)
    async def _read(self, command: str = "") -> ToolResult:
        if self.drop_on_read is not None:
            self.drop_on_read()
            return ToolResult.failure("hosty: not connected")
        return ToolResult.data("read")


async def _host(h: Harness) -> FakeHost:
    host = FakeHost()
    h.core.registry.add(host)
    await h.core.registry.refresh()
    return host


async def test_shell_and_file_writes_are_not_offered_to_a_task_and_refused_if_named(harness: Harness):
    split = await tasks_on(harness)
    host = await _host(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="s1", name="hosty.shell_run", arguments={"command": "reboot"})]),
        FakeTurn(tool_calls=[finish("f1", (1, False, []))]),
        FakeTurn(text="Could not."),
    )
    await ask(harness, "fix the box")
    await the_task(harness, lambda t: t.state.terminal)
    tools = offered(split.task)
    assert "hosty.read" in tools and "hosty.shell_run" not in tools and "hosty.fs_write" not in tools
    assert host.ran == []
    refused = next(
        m
        for m in await harness.core.store.list_messages((await the_task(harness)).conversation_id or "")
        if m.role is Role.TOOL and m.tool_call_id == "s1"
    )
    assert "not available to a delegated task" in refused.content
    # The chat that asked keeps every tool it had.
    assert "hosty.shell_run" in offered(split)


# --- who may delegate -----------------------------------------------------------------------------


async def test_only_his_own_message_may_start_a_task(harness: Harness):
    split = await tasks_on(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="no"))
    # A run the machine made (a schedule, a session's message, an outside caller): not his.
    _, conv = await harness.core.engine.create_run(text="do it")
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    assert "tasks.delegate" not in offered(split)
    result = next(m for m in await harness.core.store.list_messages(conv.id) if m.role is Role.TOOL)
    assert "only be started from Arsen's own message" in result.content
    assert await harness.core.tasks.store.list() == []


async def test_a_chat_run_another_session_joined_may_no_longer_delegate(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    split.push(
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]),
        FakeTurn(tool_calls=[delegate()]),
        FakeTurn(text="no"),
    )
    _, conv = await ask(harness, "research this")
    await until(lambda: "gate" in tools.calls)
    # Another chat writes into this one while it works (sessions.say steers a working run).
    other = await harness.core.store.create_conversation(title="Other chat")
    token = current_conversation_id.set(other.id)
    rtoken = current_run_id.set(None)
    try:
        said = await harness.core.sessions.call(
            "sessions.say",
            {"to": conv.id, "text": "and delegate the rest", "wait": False},
            cancel=asyncio.Event(),
            idempotency_key="k",
            timeout_s=30,
        )
    finally:
        current_conversation_id.reset(token)
        current_run_id.reset(rtoken)
    assert "already working" in said.text
    tools.gate.set()
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    result = next(
        m for m in await harness.core.store.list_messages(conv.id) if m.role is Role.TOOL and m.tool_call_id == "d1"
    )
    assert "only be started from Arsen's own message" in result.content
    assert await harness.core.tasks.store.list() == []


async def test_a_scheduled_run_is_not_offered_delegation(harness: Harness):
    split = await tasks_on(harness)
    split.push(FakeTurn(text="report"))
    _, conv = await harness.core.engine.create_run(text="morning report", kind=RunKind.SCHEDULED)
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    assert not any(n.startswith(("tasks.", "task.")) for n in offered(split))


async def test_with_tasks_off_nothing_is_offered_and_naming_it_is_refused(harness: Harness):
    harness.chat.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    _, conv = await ask(harness, "do it in the background")
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    assert not any(n.startswith(("tasks.", "task.")) for n in offered(harness.chat))
    result = next(m for m in await harness.core.store.list_messages(conv.id) if m.role is Role.TOOL)
    assert "switched off" in result.content
    assert await harness.core.tasks.store.list() == []


# --- honest about "can I" -------------------------------------------------------------------------


async def test_a_task_that_needs_a_machine_that_is_down_is_declined_and_not_made(harness: Harness):
    split = await tasks_on(harness)
    host = await _host(harness)
    harness.core.registry.mark_unavailable(host, "ConnectError: refused")
    split.push(FakeTurn(tool_calls=[delegate(needs=("hosty",))]), FakeTurn(text="Can't now - hosty is down."))
    _, conv = await ask(harness, "check the logs on hosty")
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    result = next(m for m in await harness.core.store.list_messages(conv.id) if m.role is Role.TOOL)
    assert "hosty is not reachable (down since" in result.content and "No task was made" in result.content
    assert await harness.core.tasks.store.list() == []


async def test_a_need_that_names_no_tools_is_refused_with_the_real_names(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    split.push(FakeTurn(tool_calls=[delegate(needs=("nowhere",))]), FakeTurn(text="ok"))
    _, conv = await ask(harness, "x")
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    result = next(m for m in await harness.core.store.list_messages(conv.id) if m.role is Role.TOOL)
    assert "no tools in nowhere" in result.content and "test" in result.content


async def test_a_machine_going_down_mid_task_ends_it_not_done_at_once(harness: Harness):
    split = await tasks_on(harness)
    host = await _host(harness)
    pushes(harness)
    host.drop_on_read = lambda: harness.core.registry.mark_unavailable(host, "connection lost")
    split.push(FakeTurn(tool_calls=[delegate(needs=("hosty",))]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="h1", name="hosty.read", arguments={})]), FakeTurn(text="never"))
    _, conv = await ask(harness, "read it")
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.NOT_DONE
    reason = task.outcome.stopped_reason if task.outcome else ""
    assert reason and reason.startswith("hosty is not reachable (down since") and "connection lost" in reason
    # Ended on the spot: no wrap-up turn around the hole.
    assert len(split.task.calls) == 1
    report = (await harness.core.store.list_messages(conv.id))[-1].content
    assert "not done" in report and "Stopped: hosty is not reachable" in report


async def test_health_block_names_what_is_down_and_is_absent_when_all_is_up(harness: Harness):
    split = await tasks_on(harness)
    host = await _host(harness)
    split.push(FakeTurn(text="all good"))
    _, conv = await ask(harness, "hi")
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    first = "\n".join(m.content for m in split.calls[0][0])
    assert "- hosty:" not in first
    harness.core.registry.mark_unavailable(host, "ConnectError: refused")
    split.push(FakeTurn(text="hosty is down"))
    await ask(harness, "is hosty up?", conv.id)
    await harness.wait_for(sub, "run.done")
    second = "\n".join(m.content for m in split.calls[1][0])
    assert "## Not reachable now" in second and "- hosty: down since" in second and "ConnectError" in second


# --- questions, steering, cancelling, budgets -------------------------------------------------------


async def test_a_task_asks_what_only_he_knows_and_his_answer_continues_it(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    sent = pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="a1", name="task.ask", arguments={"question": "What budget per night?"})]),
        FakeTurn(text="What budget per night?"),
    )
    await ask(harness, "find a hotel")
    task = await the_task(harness, lambda t: t.state is TaskState.NEEDS_INPUT)
    assert task.outcome is not None and task.outcome.question == "What budget per night?"
    assert task.report_message_id is None  # a question is not a report
    await until(lambda: any("answer" in p["title"] for p in sent))
    split.task.push(
        FakeTurn(tool_calls=[echo("c2", "under 180")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c2"]))]),
        FakeTurn(text="Done within budget."),
    )
    await harness.core.tasks.steer(task.id, "Up to 180 a night")
    task = await the_task(harness, lambda t: t.state is TaskState.DONE and t.report_message_id is not None)
    assert task.outcome is not None and task.outcome.question is None
    words = [
        m.content
        for m in await harness.core.store.list_messages(task.conversation_id or "")
        if m.role is Role.USER and m.name is None
    ]
    assert words[-1] == "Up to 180 a night"  # his answer, as he gave it, in the task's own history


async def test_steering_a_working_task_reaches_its_next_step(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]),
        FakeTurn(tool_calls=[finish("f1", (1, False, []))]),
        FakeTurn(text="."),
    )
    await ask(harness, "find hotels")
    task = await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: "gate" in tools.calls)
    await harness.core.tasks.steer(task.id, "3 nights, not 2")
    tools.gate.set()
    await the_task(harness, lambda t: t.state.terminal)
    seen = "\n".join(m.content for m in split.task.calls[1][0])
    assert "3 nights, not 2" in seen


async def test_cancelling_a_working_task_stops_it_and_reports_it_cancelled(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="s1", name="test.slow", arguments={})]))
    _, conv = await ask(harness, "find hotels")
    task = await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: len(split.task.calls) == 1)
    await harness.core.tasks.cancel(task.id)
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.CANCELLED
    assert "Task: Hotels — cancelled" in (await harness.core.store.list_messages(conv.id))[-1].content


async def test_a_task_that_runs_out_of_budget_is_not_done_never_done(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    harness.enable(budgets={**harness.core.settings.budgets, RunKind.TASK: RunBudget(max_steps=2)})
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[echo("c2", "y")]),
        FakeTurn(text="Here is what I found so far."),  # the wrap-up
    )
    _, conv = await ask(harness, "big research")
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.NOT_DONE
    assert task.outcome is not None and "step budget reached" in (task.outcome.stopped_reason or "")
    assert task.outcome.dod[0].met is None
    report = (await harness.core.store.list_messages(conv.id))[-1].content
    assert "not reported" in report and "Here is what I found so far." in report


# --- several at once, the GPU, the call ----------------------------------------------------------------


async def test_two_asks_in_one_breath_are_two_tasks_and_only_one_runs_at_a_time(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(
        FakeTurn(tool_calls=[delegate("d1", title="Hotels"), delegate("d2", title="Trains")]),
        FakeTurn(text="Two tasks."),
    )
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["g1"]))]),
        FakeTurn(text="hotels"),
        FakeTurn(tool_calls=[echo("e2")]),
        FakeTurn(tool_calls=[finish("f2", (1, True, ["e2"]))]),
        FakeTurn(text="trains"),
    )
    await ask(harness, "book a hotel and check the trains")
    await until(lambda: "gate" in tools.calls)
    tasks = {t.title: t for t in await harness.core.tasks.store.list()}
    assert set(tasks) == {"Hotels", "Trains"}
    assert tasks["Trains"].state is TaskState.QUEUED  # tasks.max_running = 1
    tools.gate.set()
    await until(lambda: _all_done(harness, 2))
    assert {t.state for t in await harness.core.tasks.store.list()} == {TaskState.DONE}


async def _all_done(h: Harness, n: int) -> bool:
    tasks = await h.core.tasks.store.list()
    return len(tasks) == n and all(t.report_message_id for t in tasks)


async def test_no_task_starts_during_a_call_and_a_running_one_holds_between_steps(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    on_call = [True]
    harness.core.tasks.call_open = lambda: on_call[0]  # type: ignore[method-assign]
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["g1"]))]),
        FakeTurn(text="done"),
    )
    await ask(harness, "research")
    task = await the_task(harness)
    await asyncio.sleep(0.3)
    assert (await harness.core.tasks.store.get(task.id)).state is TaskState.QUEUED and split.task.calls == []
    on_call[0] = False
    harness.core.engine.wake()
    await until(lambda: "gate" in tools.calls)
    watcher = harness.subscribe("elsewhere")
    on_call[0] = True  # he calls while it works
    tools.gate.set()
    await until(
        lambda: any(getattr(e, "type", "") == "task.changed" and e.task.paused for e in list(watcher.queue._queue))
    )  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    await asyncio.sleep(0.3)
    assert len(split.task.calls) == 1  # held between steps: no model call beside the call
    on_call[0] = False
    task = await the_task(harness, lambda t: t.state is TaskState.DONE)
    assert not task.paused


# --- the report's place, files, privacy, restarts ---------------------------------------------------------


async def test_the_report_waits_for_the_chat_to_fall_quiet(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(
        FakeTurn(tool_calls=[delegate(), ToolCall(id="g1", name="test.gate", arguments={})]),
        FakeTurn(text="And here is the rest of my answer."),
    )
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]), FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]), FakeTurn(text="t")
    )
    _, conv = await ask(harness, "two things")
    task = await the_task(harness, lambda t: t.state.terminal)
    await asyncio.sleep(0.2)
    assert (await harness.core.tasks.store.get(task.id)).report_message_id is None
    assert not any(m.task_id for m in await harness.core.store.list_messages(conv.id))
    tools.gate.set()
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    msgs = await harness.core.store.list_messages(conv.id)
    # After the chat's own answer, never in the middle of the run that was writing it.
    answer = next(i for i, m in enumerate(msgs) if m.content == "And here is the rest of my answer.")
    report = next(i for i, m in enumerate(msgs) if m.id == task.report_message_id)
    assert report > answer


async def test_files_the_task_attached_travel_with_the_report(harness: Harness):
    split = await tasks_on(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    attach = ToolCall(id="a1", name="files.attach", arguments={"filename": "hotels.md", "content": "# Hotels"})
    split.task.push(
        FakeTurn(tool_calls=[attach]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["a1"]))]),
        FakeTurn(text="list attached"),
    )
    await ask(harness, "a list please")
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.DONE  # files.attach needs no approval (settings.tasks.no_approval)
    files = await harness.core.attachments.for_message(task.report_message_id or "")
    assert [f.name for f in files] == ["hotels.md"]
    assert (await harness.core.attachments.get(files[0].id)) is not None


async def test_an_incognito_chats_task_is_as_private_as_the_chat(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    sent = pushes(harness)
    secret = await harness.core.store.create_conversation(title="private", incognito=True)
    split.push(FakeTurn(tool_calls=[delegate(title="Gift for Maria")]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]), summary="A scarf, 40 EUR.")]),
        FakeTurn(text="."),
    )
    await ask(harness, "find a gift for Maria", secret.id)
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.incognito
    own = await harness.core.store.get_conversation(task.conversation_id or "")
    assert own is not None and own.incognito
    # The notice carries no words from it - not the title, not the summary.
    assert all("Maria" not in p["title"] and "scarf" not in p["body"] for p in sent)
    listed = await harness.core.sessions.call(
        "sessions.list", {"today": True}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5
    )
    assert "Gift for Maria" not in listed.text


async def test_a_restart_while_a_task_waits_for_approval_loses_nothing(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate(dod=("it is written",))]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="w1", name="test.write", arguments={})]))
    _, conv = await ask(harness, "write it")
    task = await the_task(harness, lambda t: t.state is TaskState.WAITING)
    await harness.restart()
    await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    await harness.core.tasks.reconcile()
    waiting = await harness.core.tasks.store.get(task.id)
    assert waiting is not None and waiting.state is TaskState.WAITING and waiting.waiting is not None
    split.task.push(FakeTurn(tool_calls=[finish("f1", (1, True, ["w1"]))]), FakeTurn(text="written"))
    await harness.core.engine.confirm(waiting.waiting.run_id, waiting.waiting.call_id, True)
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert task.state is TaskState.DONE and tools.calls == ["write"]
    await asyncio.sleep(0.2)
    assert (
        sum(1 for m in await harness.core.store.list_messages(conv.id) if m.task_id and m.role is Role.ASSISTANT) == 1
    )


async def test_a_round_that_ended_while_the_service_was_down_is_finished_on_reconcile(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    started = asyncio.Event()
    split.task.on_call = lambda n: started.set()
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]), FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]), FakeTurn(text="done")
    )
    await ask(harness, "x")
    await started.wait()
    await harness.core.tasks.stop()  # the events of the rest of the run are missed
    task = await the_task(harness)

    async def run_ended() -> bool:
        run = await harness.core.store.get_run(task.run_id or "")
        return run is not None and run.status.terminal

    await until(run_ended)
    assert not (await harness.core.tasks.store.get(task.id)).state.terminal
    await harness.core.tasks.reconcile()
    task = await harness.core.tasks.store.get(task.id)
    assert task is not None and task.state is TaskState.DONE and task.report_message_id


async def test_an_approval_nobody_answers_expires_with_a_re_read_note(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="w1", name="test.write", arguments={})]))
    await ask(harness, "write")
    task = await the_task(harness, lambda t: t.state is TaskState.WAITING)
    split.task.push(FakeTurn(tool_calls=[finish("f1", (1, False, []))]), FakeTurn(text="It expired."))
    await harness.core.tasks.sweep(now=datetime.now(UTC) + timedelta(hours=1))
    assert (await harness.core.tasks.store.get(task.id)).state is TaskState.WAITING  # not yet
    await harness.core.tasks.sweep(now=datetime.now(UTC) + timedelta(hours=5))
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    assert tools.calls == [] and task.state is TaskState.NOT_DONE and task.note == "an approval expired unanswered"
    result = next(m for m in split.task.calls[1][0] if m.role is Role.TOOL and m.tool_call_id == "w1")
    assert "approval expired" in result.content and "re-read the source" in result.content


async def test_deleting_the_chat_takes_its_tasks_with_it(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]), FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]), FakeTurn(text=".")
    )
    _, conv = await ask(harness, "x")
    task = await the_task(harness, lambda t: t.report_message_id is not None)
    watcher = harness.subscribe("elsewhere")
    await harness.core.delete_conversation(conv.id)
    assert await harness.core.store.get_conversation(task.conversation_id or "") is None
    assert await harness.core.tasks.store.get(task.id) is None
    assert any(getattr(e, "type", "") == "task.removed" for e in drain(watcher))


async def test_the_chats_next_turn_sees_its_open_tasks(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(FakeTurn(tool_calls=[ToolCall(id="g1", name="test.gate", arguments={})]))
    _, conv = await ask(harness, "find hotels")
    task = await the_task(harness, lambda t: t.state is TaskState.RUNNING)
    await until(lambda: "gate" in tools.calls)
    split.push(FakeTurn(text="Still on the hotels."))
    sub = harness.subscribe(conv.id)
    await ask(harness, "what are you working on?", conv.id)
    await harness.wait_for(sub, "run.done")
    context = "\n".join(m.content for m in split.calls[-1][0])
    assert "## Delegated tasks" in context and f"{task.id} · Hotels — working" in context
    tools.gate.set()


# --- REST ---------------------------------------------------------------------------------------------


def test_tasks_over_rest(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="t"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    h = {"Authorization": "Bearer t"}
    with TestClient(create_app(core.config, core=core)) as client:
        conv = client.post("/api/conversations", json={"title": "Trip"}, headers=h).json()

        async def make() -> Task:
            task = Task(id="task_1", title="Hotels", state=TaskState.NOTED, origin_conversation_id=conv["id"])
            return await core.tasks.store.create(task, origin_key="r:c")

        task = client.portal.call(make)  # type: ignore[union-attr]
        assert client.get("/api/tasks", headers=h).status_code == 200
        listed = client.get(f"/api/tasks?conversation_id={conv['id']}&open=1", headers=h).json()
        assert [t["id"] for t in listed] == [task.id] and listed[0]["state"] == "noted"
        assert client.get(f"/api/tasks/{task.id}", headers=h).json()["title"] == "Hotels"
        assert client.get("/api/tasks/nope", headers=h).status_code == 404
        assert client.post("/api/tasks/nope/cancel", headers=h).status_code == 404
        # A noted task he steers keeps his words for the brief; cancelled, it is dropped unstarted.
        steered = client.post(f"/api/tasks/{task.id}/steer", json={"text": "near the Rathaus"}, headers=h).json()
        assert steered["brief"]["asked"] == ["near the Rathaus"]
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=h).json()["state"] == "dropped"
        assert client.get(f"/api/tasks?conversation_id={conv['id']}&open=1", headers=h).json() == []
        assert client.get("/api/status", headers=h).json()["tasks"]["enabled"] is False
        assert client.get("/api/tasks").status_code == 401


async def test_with_everything_up_the_health_block_costs_nothing(harness: Harness):
    harness.enable(tasks=TasksSettings(enabled=True))

    class AllUp:
        def failing(self) -> list[tuple[str, str, datetime]]:
            return []

    block = HealthBlock(AllUp, lambda: harness.core.settings)
    run = Run(id="r", conversation_id="c", kind=RunKind.CHAT)
    assert await block.context_block(run, skill_names=[]) is None
