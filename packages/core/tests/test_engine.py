"""Cancellation, restart/resume, queueing, unread."""

from __future__ import annotations

import asyncio
import time

from jarvis_core.models.fake import FakeTurn
from jarvis_proto import RunStatus, ToolCall
from tests.conftest import Harness
from tests.test_loop import with_tools


async def test_cancel_during_stream_keeps_partial_text(harness: Harness):
    harness.chat.push(FakeTurn(text="one two three four five six seven eight", token_delay_s=0.05))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "model.delta")
    t0 = time.perf_counter()
    assert await harness.core.engine.cancel(run.id)
    seen = await harness.wait_for(sub, "run.cancelled")
    assert time.perf_counter() - t0 < 1.0
    cancelled = seen[-1]
    assert cancelled.partial_message_id is not None
    msgs = await harness.core.store.list_messages(conv.id)
    assert msgs[-1].partial is True and msgs[-1].content.startswith("one")
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.CANCELLED
    # The run.updated announcing 'cancelling' preceded the cancellation.
    assert any(getattr(e, "type", "") == "run.updated" and e.run.status is RunStatus.CANCELLING for e in seen)


async def test_cancel_during_slow_tool(harness: Harness):
    await with_tools(harness)
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.slow", arguments={})]))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "tool.call")
    t0 = time.perf_counter()
    await harness.core.engine.cancel(run.id)
    await harness.wait_for(sub, "run.cancelled")
    assert time.perf_counter() - t0 < 1.0


async def test_cancel_while_queued(harness: Harness):
    harness.chat.push(FakeTurn(hang=True), FakeTurn(text="never"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.core.engine._max = 1
    first, _ = await harness.core.engine.create_run(text="a", conversation_id=conv.id)
    await harness.wait_for(sub, "model.call")
    second, _ = await harness.core.engine.create_run(text="b", conversation_id=conv.id)
    assert harness.core.engine.queued_count() == 1
    assert await harness.core.engine.cancel(second.id)
    assert (await harness.core.store.get_run(second.id)).status is RunStatus.CANCELLED
    await harness.core.engine.cancel(first.id)
    await harness.wait_for(sub, "run.cancelled")


async def test_restart_during_read_only_tool_resumes_and_finishes(harness: Harness):
    tools = await with_tools(harness)
    # The model asks for a slow read-only tool; we "crash" while it runs.
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.slow", arguments={})]))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "tool.call")

    # Make the tool succeed instantly after the restart, then let the model finish.
    async def quick(**_: object):
        from jarvis_proto import ToolResult

        return ToolResult.data("resumed-result")

    await harness.restart()
    tools2 = await with_tools(harness)
    tools2._entries["test.slow"].fn = quick
    harness.chat.push(FakeTurn(text="finished after restart"))
    sub2 = harness.subscribe(conv.id)
    await harness.wait_for(sub2, "run.done", timeout=10)
    # Asserted against the event LOG, not against what this subscriber happened to catch: the
    # engine resumes the moment it starts, which is before any client can attach to the new
    # process's bus, so a reconnecting client learns about the resume by replaying events.
    stored = [e["type"] for e in await harness.core.store.list_events(run.id)]
    assert "run.interrupted" in stored and "run.resumed" in stored and stored[-1] == "run.done"
    msgs = await harness.core.store.list_messages(conv.id)
    assert [m.role.value for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[2].content == "resumed-result" and msgs[-1].content == "finished after restart"
    assert tools.calls == []  # the original instance never completed


async def test_restart_during_mutating_tool_waits_for_the_user(harness: Harness):
    tools = await with_tools(harness)
    slow_write_called = asyncio.Event()

    async def slow_write(**_: object):
        from jarvis_proto import ToolResult

        slow_write_called.set()
        await asyncio.sleep(30)
        return ToolResult.data("never")

    tools._entries["test.write"].fn = slow_write
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.write", arguments={})]))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="write", conversation_id=conv.id)
    await harness.wait_for(sub, "tool.call")
    await slow_write_called.wait()

    await harness.restart()
    tools2 = await with_tools(harness)
    sub2 = harness.subscribe(conv.id)
    seen = await harness.wait_for(sub2, "run.waiting_user", timeout=10)
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert req.call_id == "c1" and "interrupted" in req.reason
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.WAITING_USER

    # User says "it did not happen, do it again": the retry uses a fresh idempotency key.
    harness.chat.push(FakeTurn(text="written after confirmation"))
    assert await harness.core.engine.confirm(run.id, "c1", True)
    seen = await harness.wait_for(sub2, "run.done", timeout=10)
    assert tools2.calls == ["write"]
    call = next(e for e in seen if e.type == "tool.call")
    assert call.idempotency_key.endswith(":retry")


async def test_confirmation_survives_a_restart_while_waiting(harness: Harness):
    tools = await with_tools(harness)
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="send", conversation_id=conv.id)
    await harness.wait_for(sub, "run.waiting_user")

    await harness.restart()  # waiting_user runs stay parked
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.WAITING_USER
    tools2 = await with_tools(harness)
    harness.chat.push(FakeTurn(text="sent"))
    sub2 = harness.subscribe(conv.id)
    assert await harness.core.engine.confirm(run.id, "c1", True)
    await harness.wait_for(sub2, "run.done", timeout=10)
    assert tools2.calls == ["send"] and tools.calls == []


async def test_priority_and_concurrency(harness: Harness):
    harness.core.engine._max = 1
    order: list[str] = []
    harness.chat.on_call = lambda n: order.append(harness.chat.calls[-1][0][-1].content)
    harness.chat.push(FakeTurn(hang=True), FakeTurn(text="x"), FakeTurn(text="y"))
    # Separate conversations so the last message of each context is that run's own input.
    c1 = await harness.core.store.create_conversation()
    c2 = await harness.core.store.create_conversation()
    c3 = await harness.core.store.create_conversation()
    sub = harness.subscribe(c1.id)
    first, _ = await harness.core.engine.create_run(text="first", conversation_id=c1.id)
    await harness.wait_for(sub, "model.call")
    from jarvis_proto import RunKind

    await harness.core.engine.create_run(text="scheduled", conversation_id=c2.id, kind=RunKind.SCHEDULED)
    await harness.core.engine.create_run(text="chat", conversation_id=c3.id)
    await harness.core.engine.cancel(first.id)
    await harness.wait_for(sub, "run.cancelled")
    await asyncio.sleep(0.3)
    assert order[:3] == ["first", "chat", "scheduled"]


async def test_unread_is_set_only_when_nobody_is_watching(harness: Harness):
    harness.chat.push(FakeTurn(text="a"), FakeTurn(text="b"))
    watched = await harness.core.store.create_conversation()
    sub = harness.subscribe(watched.id)
    await harness.core.engine.create_run(text="hi", conversation_id=watched.id)
    await harness.wait_for(sub, "run.done")
    assert (await harness.core.store.get_conversation(watched.id)).unread is False

    unwatched = await harness.core.store.create_conversation()
    global_sub = harness.subscribe("nothing")
    await harness.core.engine.create_run(text="hi", conversation_id=unwatched.id)
    # conversation.updated is broadcast to everyone; the unread flag rides on it.
    async with asyncio.timeout(5):
        while True:
            ev = await global_sub.queue.get()
            if (
                getattr(ev, "type", "") == "conversation.updated"
                and ev.conversation.id == unwatched.id
                and ev.conversation.unread
            ):
                break
    assert (await harness.core.store.get_conversation(unwatched.id)).unread is True


async def test_new_conversation_is_titled_from_the_first_message(harness: Harness):
    harness.chat.push(FakeTurn(text="ok"))
    sub = harness.subscribe("x")
    _run, conv = await harness.core.engine.create_run(text="Draft the weekly status for Rumen\nwith numbers")
    assert conv.title == "Draft the weekly status for Rumen"
    updated = [
        e for e in [sub.queue.get_nowait() for _ in range(sub.queue.qsize())] if e.type == "conversation.updated"
    ]
    assert updated and updated[0].conversation.id == conv.id
