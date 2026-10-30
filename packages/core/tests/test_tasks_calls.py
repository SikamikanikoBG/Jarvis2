"""Tasks noted on a call (docs/stories/13_delegated_tasks.md, slice 2): the call keeps being a
conversation, what was noted starts exactly once when it ends, and one report says how it went."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.context import VoiceBlock
from jarvis_core.features.tasks import voice_style_upgrade
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import Channel, Role, RoleName, Run, RunKind, Settings, TasksSettings, TaskState, ToolCall
from jarvis_proto.settings import CALL_WORK_NOW, VOICE_STYLE_DEFAULT
from tests.conftest import Harness
from tests.test_loop import with_tools
from tests.test_tasks import delegate, echo, finish, offered, pushes, reported, tasks_on, the_task, until

CALL = "call_1"

# Arsen's live style as panel-checked on 2026-10-29: the old default with his own lines after it.
LIVE_STYLE = (
    VOICE_STYLE_DEFAULT.replace("Each time you are about", f"{CALL_WORK_NOW} Each time you are about")
    + " Always answer me in English, whatever language I speak. Read numbers the way a person says them."
)


def note(call_id: str, title: str, needs: tuple[str, ...] = ()) -> ToolCall:
    return ToolCall(id=call_id, name="tasks.note", arguments={"title": title, "needs": list(needs)})


def brief(call_id: str = "b1", dod: tuple[str, ...] = ("three hotels",)) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="task.brief",
        arguments={
            "goal": "hotels near the Rathaus",
            "context": "14-16 Nov, two adults",
            "dod": [{"item": d} for d in dod],
            "assumptions": ["under 180 a night"],
        },
    )


async def on_the_call(h: Harness, text: str, conversation_id: str | None = None, call_id: str = CALL):
    await h.core.tasks.call_seen(call_id, conversation_id)
    return await h.core.engine.create_run(
        text=text, conversation_id=conversation_id, channel=Channel.VOICE, call_id=call_id, from_owner=True
    )


async def noted_on_a_call(h: Harness, *titles: str) -> str:
    """A call in which each title is noted in a turn of its own; returns the conversation."""
    split = h.chat
    conv_id: str | None = None
    for i, title in enumerate(titles):
        split.push(FakeTurn(tool_calls=[note(f"n{i}", title)]), FakeTurn(text="Noted."))
        _, conv = await on_the_call(h, f"and {title.lower()} please", conv_id)
        conv_id = conv.id
        sub = h.subscribe(conv.id)
        await h.wait_for(sub, "run.done")
        h.core.bus.detach(sub)
    assert conv_id is not None
    return conv_id


async def test_on_a_call_work_is_noted_with_his_words_and_nothing_runs(harness: Harness):
    split = await tasks_on(harness)
    conv_id = await noted_on_a_call(harness, "Hotels Vienna")
    task = await the_task(harness)
    assert task.state is TaskState.NOTED and task.call_id == CALL and task.run_id is None
    assert task.brief.asked == ["and hotels vienna please"] and not task.brief.written
    assert split.task.calls == []
    # On a call the tool is tasks.note; tasks.delegate is for a chat.
    tools = offered(split)
    assert "tasks.note" in tools and "tasks.delegate" not in tools
    # The next turn of the call has the noted list, in his words, and the policy that replaces
    # "never after the call".
    split.push(FakeTurn(text="So far: the hotels."))
    _, _conv = await on_the_call(harness, "what have you got so far?", conv_id)
    sub = harness.subscribe(conv_id)
    await harness.wait_for(sub, "run.done")
    context = "\n".join(m.content for m in split.calls[-1][0])
    assert "Noted on this call so far" in context and '"and hotels vienna please"' in context
    assert "takes precedence over any line in the call style" in context
    assert CALL_WORK_NOW not in context


async def test_a_typed_chat_is_offered_delegate_not_note(harness: Harness):
    split = await tasks_on(harness)
    split.push(FakeTurn(text="hi"))
    _, conv = await harness.core.engine.create_run(text="hi", from_owner=True)
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    assert "tasks.delegate" in offered(split) and "tasks.note" not in offered(split)


async def test_noted_tasks_start_exactly_once_on_a_double_hang_up(harness: Harness):
    split = await tasks_on(harness)
    pushes(harness)
    await noted_on_a_call(harness, "Hotels", "Trains")
    started = await asyncio.gather(
        harness.core.tasks.call_ended(CALL, how="hangup"), harness.core.tasks.call_ended(CALL, how="hangup")
    )
    assert sorted(len(s) for s in started) == [0, 2]
    # ...and the sweeper later finds nothing more to start.
    await harness.core.tasks.sweep(now=datetime.now(UTC) + timedelta(hours=1))
    tasks = await harness.core.tasks.store.list()
    runs = [r for t in tasks for r in await harness.core.store.list_runs(t.conversation_id or "")]
    assert len(runs) == 2 and all(t.state is not TaskState.NOTED for t in tasks)
    del split


async def test_a_dropped_call_is_ended_by_the_sweeper_only_after_the_drop_window(harness: Harness):
    await tasks_on(harness, call_drop_after_s=240)
    pushes(harness)
    await noted_on_a_call(harness, "Hotels")
    now = datetime.now(UTC)
    await harness.core.tasks.sweep(now=now + timedelta(seconds=200))  # quiet, not dropped
    assert (await the_task(harness)).state is TaskState.NOTED
    assert harness.core.tasks.call_open()
    await harness.core.tasks.call_seen(CALL, None)  # the heartbeat
    await harness.core.tasks.sweep(now=datetime.now(UTC) + timedelta(seconds=200))
    assert (await the_task(harness)).state is TaskState.NOTED
    await harness.core.tasks.sweep(now=datetime.now(UTC) + timedelta(seconds=241))
    task = await the_task(harness)
    assert task.state is not TaskState.NOTED and task.run_id is not None
    row = await harness.core.db.fetchone("SELECT end_reason FROM calls WHERE id = ?", (CALL,))
    assert row is not None and row["end_reason"] == "drop"
    # A second end, by hand, starts nothing more.
    assert await harness.core.tasks.call_ended(CALL, how="hangup") == []


async def test_a_call_that_comes_back_after_a_drop_keeps_its_new_notes_for_its_end(harness: Harness):
    split = await tasks_on(harness)
    pushes(harness)
    conv = await noted_on_a_call(harness, "Hotels")
    await harness.core.tasks.sweep(now=datetime.now(UTC) + timedelta(seconds=300))  # taken for dropped
    first = await the_task(harness, lambda t: t.title == "Hotels")
    assert first.state is not TaskState.NOTED
    # The phone reconnects: the same call speaks again and notes one more thing.
    split.push(FakeTurn(tool_calls=[note("n9", "Trains")]), FakeTurn(text="Noted."))
    await on_the_call(harness, "and the trains", conv)
    trains = await the_task(harness, lambda t: t.title == "Trains" and t.state is TaskState.NOTED)
    assert harness.core.tasks.call_open()
    started = await harness.core.tasks.call_ended(CALL, how="hangup")
    assert started == [trains.id]


async def test_a_restart_between_noting_and_hanging_up_still_starts_them_once(harness: Harness):
    await tasks_on(harness)
    await noted_on_a_call(harness, "Hotels")
    await harness.restart()
    await tasks_on(harness)
    pushes(harness)
    await harness.core.tasks.reconcile()
    assert harness.core.tasks.call_open()  # still open after the restart, until it ends
    assert (await the_task(harness)).state is TaskState.NOTED
    assert len(await harness.core.tasks.call_ended(CALL, how="hangup")) == 1
    assert await harness.core.tasks.call_ended(CALL, how="hangup") == []


async def test_noted_on_a_call_that_ended_while_the_core_was_down_start_on_reconcile(harness: Harness):
    await tasks_on(harness)
    await noted_on_a_call(harness, "Hotels")
    await harness.core.db.execute(
        "UPDATE calls SET ended_at = ?, end_reason = 'hangup'", (datetime.now(UTC).isoformat(),)
    )
    await harness.restart()
    await tasks_on(harness)
    pushes(harness)
    await harness.core.tasks.reconcile()
    assert (await the_task(harness)).state is not TaskState.NOTED


async def test_no_task_starts_while_a_call_is_open(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    await harness.core.tasks.call_seen("call_x", None)  # a call is going on, between its turns
    split.push(FakeTurn(tool_calls=[delegate()]), FakeTurn(text="ok"))
    split.task.push(
        FakeTurn(tool_calls=[echo("c1")]), FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]), FakeTurn(text=".")
    )
    await harness.core.engine.create_run(text="find hotels", from_owner=True)
    task = await the_task(harness)
    await asyncio.sleep(0.3)
    assert (await harness.core.tasks.store.get(task.id)).state is TaskState.QUEUED
    assert split.task.calls == []
    await harness.core.tasks.call_ended("call_x", how="hangup")
    await the_task(harness, lambda t: t.state is TaskState.DONE)


async def test_a_noted_task_writes_its_brief_first_from_the_call_and_his_words(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    pushes(harness)
    await noted_on_a_call(harness, "Hotels")
    split.task.push(
        FakeTurn(tool_calls=[echo("c0")]),  # before the brief: refused
        FakeTurn(tool_calls=[brief()]),
        FakeTurn(tool_calls=[echo("c1")]),
        FakeTurn(tool_calls=[finish("f1", (1, True, ["c1"]))]),
        FakeTurn(text="Three hotels."),
    )
    await harness.core.tasks.call_ended(CALL, how="hangup")
    task = await reported(harness)
    first = split.task.calls[0][0]
    opening = next(m.content for m in first if m.role is Role.USER and m.name is None)
    assert "Its brief is not written yet" in opening
    assert (
        "## Where he asked it" in opening and "Arsen: and hotels please" in opening and "Jarvis: Noted." in opening
    )  # the call, copied by code
    refused = next(
        m for m in await harness.core.store.list_messages(task.conversation_id or "") if m.tool_call_id == "c0"
    )
    assert "write the brief first" in refused.content
    assert task.brief.written and task.brief.dod[0].item == "three hotels"
    assert task.brief.assumptions == ["under 180 a night"]
    assert task.state is TaskState.DONE


async def test_one_after_call_report_for_everything_noted_on_the_call(harness: Harness):
    split = await tasks_on(harness)
    await with_tools(harness)
    sent = pushes(harness)
    conv = await noted_on_a_call(harness, "Hotels", "Trains")
    for i in (1, 2):
        split.task.push(
            FakeTurn(tool_calls=[brief(f"b{i}")]),
            FakeTurn(tool_calls=[echo(f"c{i}")]),
            FakeTurn(tool_calls=[finish(f"f{i}", (1, True, [f"c{i}"]))]),
            FakeTurn(text="done"),
        )
    await harness.core.tasks.call_ended(CALL, how="hangup")
    await until(lambda: _reported(harness, 2))
    reports = [m for m in await harness.core.store.list_messages(conv) if m.task_id and m.role is Role.ASSISTANT]
    assert len(reports) == 1
    text = reports[0].content
    assert (
        text.startswith("**After the call — 2 done**")
        and "Task: Hotels — done" in text
        and "Task: Trains — done" in text
    )
    await until(lambda: any(p["title"].startswith("After the call") for p in sent))  # the notice follows the message
    assert [p["title"] for p in sent if p["title"].startswith("After the call")] == ["After the call: 2 done"]
    assert not any(p["title"].startswith("Task done") for p in sent)  # no push per task on top


async def _reported(h: Harness, n: int) -> bool:
    tasks = await h.core.tasks.store.list()
    ids = {t.report_message_id for t in tasks}
    if len(tasks) != n or None in ids:
        return False
    marks = ",".join("?" for _ in ids)
    return len(await h.core.db.fetchall(f"SELECT id FROM messages WHERE id IN ({marks})", tuple(ids))) == len(ids)


async def test_a_task_still_waiting_after_the_call_is_listed_and_reports_on_its_own_later(harness: Harness):
    split = await tasks_on(harness)
    tools = await with_tools(harness)
    pushes(harness)
    conv = await noted_on_a_call(harness, "Write it")
    split.task.push(
        FakeTurn(tool_calls=[brief()]), FakeTurn(tool_calls=[ToolCall(id="w1", name="test.write", arguments={})])
    )
    await harness.core.tasks.call_ended(CALL, how="hangup")
    task = await the_task(harness, lambda t: t.state is TaskState.WAITING)

    async def summary_written() -> bool:
        return any(m.content.startswith("**After the call") for m in await harness.core.store.list_messages(conv))

    await until(summary_written)
    after = next(m for m in await harness.core.store.list_messages(conv) if m.content.startswith("**After the call"))
    assert "Task: Write it — waiting for your approval" in after.content and "OK on test.write" in after.content
    split.task.push(FakeTurn(tool_calls=[finish("f1", (1, True, ["w1"]))]), FakeTurn(text="written"))
    assert task.waiting is not None
    await harness.core.engine.confirm(task.waiting.run_id, task.waiting.call_id, True)
    task = await reported(harness)
    assert task.state is TaskState.DONE and tools.calls == ["write"]
    reports = [m for m in await harness.core.store.list_messages(conv) if m.task_id and m.role is Role.ASSISTANT]
    assert len(reports) == 2 and reports[-1].content.startswith("**Task: Write it — done**")


async def test_dropping_and_amending_a_noted_task_on_the_call(harness: Harness):
    split = await tasks_on(harness)
    conv = await noted_on_a_call(harness, "Hotels", "Trains")
    hotels = await the_task(harness, lambda t: t.title == "Hotels")
    trains = await the_task(harness, lambda t: t.title == "Trains")
    split.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="x1", name="tasks.drop", arguments={"task_id": trains.id}),
                ToolCall(id="x2", name="tasks.amend", arguments={"task_id": hotels.id, "change": "3 nights, not 2"}),
            ]
        ),
        FakeTurn(text="Done."),
    )
    await on_the_call(harness, "forget the trains, and 3 nights for the hotel", conv)
    sub = harness.subscribe(conv)
    await harness.wait_for(sub, "run.done")
    assert (await harness.core.tasks.store.get(trains.id)).state is TaskState.DROPPED
    amended = await harness.core.tasks.store.get(hotels.id)
    assert amended is not None and "3 nights, not 2" in amended.brief.context
    assert amended.brief.asked[-1] == "forget the trains, and 3 nights for the hotel"
    started = await harness.core.tasks.call_ended(CALL, how="hangup")
    assert started == [hotels.id]


async def test_with_tasks_off_a_call_records_nothing_and_keeps_its_old_rule(harness: Harness):
    harness.chat.push(FakeTurn(text="On it now."))
    await harness.core.tasks.call_seen(CALL, None)
    assert not harness.core.tasks.call_open()
    _, conv = await harness.core.engine.create_run(
        text="find hotels", channel=Channel.VOICE, call_id=CALL, from_owner=True
    )
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    context = "\n".join(m.content for m in harness.chat.calls[0][0])
    assert CALL_WORK_NOW in context  # the call does everything on the line, as before tasks
    assert "## Delegated tasks" not in context
    run = (await harness.core.store.list_runs(conv.id))[0]
    assert run.call_id == CALL


async def test_the_voice_block_says_the_old_rule_only_with_tasks_off():
    s = Settings()
    voice = Run(id="r", conversation_id="c", kind=RunKind.CHAT, channel=Channel.VOICE)
    off = await VoiceBlock(lambda: s).context_block(voice, skill_names=[])
    assert off is not None and off.count(CALL_WORK_NOW) == 1
    on = Settings(tasks=TasksSettings(enabled=True))
    with_tasks = await VoiceBlock(lambda: on).context_block(voice, skill_names=[])
    assert with_tasks is not None and CALL_WORK_NOW not in with_tasks


# --- the stored call style --------------------------------------------------------------------------


def test_the_old_sentence_is_taken_out_of_his_live_style_and_his_own_lines_are_kept():
    stored = Settings.model_validate({"voice": {"style": LIVE_STYLE}})
    assert stored.voice.style_version == 1
    updated, note = voice_style_upgrade(stored)
    assert note is None and updated is not None
    assert CALL_WORK_NOW not in updated.voice.style
    assert "Always answer me in English, whatever language I speak." in updated.voice.style
    assert "Read numbers the way a person says them." in updated.voice.style
    assert "  " not in updated.voice.style
    assert updated.voice.style_version == 2
    # Once is once: the upgraded style is left alone from then on.
    assert voice_style_upgrade(updated) == (None, None)


def test_a_style_without_the_sentence_is_left_alone_with_a_note():
    custom = Settings.model_validate({"voice": {"style": "Short answers. English only."}})
    updated, note = voice_style_upgrade(custom)
    assert updated is None and note is not None and "Saving the style here clears this note" in note
    assert custom.voice.style == "Short answers. English only."


def test_the_new_default_is_simply_marked_current():
    updated, note = voice_style_upgrade(Settings())
    assert note is None and updated is not None
    assert updated.voice.style == VOICE_STYLE_DEFAULT and updated.voice.style_version == 2
    assert CALL_WORK_NOW not in VOICE_STYLE_DEFAULT


def test_the_core_upgrades_the_stored_style_at_start_and_rest_says_so(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="t"))
    core.adapters.fakes = {r: FakeAdapter() for r in RoleName}
    h = {"Authorization": "Bearer t"}

    async def seed() -> None:
        await core.db.open()
        await core.store.save_settings(Settings.model_validate({"voice": {"style": LIVE_STYLE}, "mcp_servers": []}))
        await core.db.close()

    asyncio.run(seed())
    with TestClient(create_app(core.config, core=core)) as client:
        voice = client.get("/api/settings", headers=h).json()["voice"]
        assert CALL_WORK_NOW not in voice["style"] and voice["style_version"] == 2
        assert "Always answer me in English" in voice["style"]
        status = client.get("/api/tasks/status", headers=h).json()
        assert status == {"enabled": False, "calls_open": 0, "voice_style_note": None}
        assert client.post("/api/calls/c9/end", headers=h).status_code == 204
        assert client.post("/api/calls/c9/end").status_code == 401


def test_a_call_over_the_websocket_carries_its_id_and_ends_by_hang_up(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=None))
    fake = FakeAdapter([FakeTurn(text="Hello.")])
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as client:
        core.apply_settings(core.settings.model_copy(update={"tasks": TasksSettings(enabled=True), "mcp_servers": []}))
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "call.alive", "call_id": "web_1", "conversation_id": None})
            ws.send_json(
                {"type": "run.create", "conversation_id": None, "text": "hi", "channel": "voice", "call_id": "web_1"}
            )
            seen: list[dict[str, Any]] = []
            while not any(e.get("type") == "run.done" for e in seen):
                seen.append(ws.receive_json())
            run_id = next(e["run_id"] for e in seen if e.get("type") == "run.queued")
            assert core.tasks.call_open()
            ws.send_json({"type": "call.ended", "call_id": "web_1"})
            ws.send_json({"type": "ping"})
            while ws.receive_json().get("type") != "pong":
                pass
            assert not core.tasks.call_open()

        async def check() -> tuple[Run | None, Any]:
            run = await core.store.get_run(run_id)
            row = await core.db.fetchone("SELECT end_reason FROM calls WHERE id = 'web_1'")
            return run, row

        run, row = client.portal.call(check)  # type: ignore[union-attr]
        assert run is not None and run.call_id == "web_1" and run.from_owner
        assert row is not None and row["end_reason"] == "hangup"
