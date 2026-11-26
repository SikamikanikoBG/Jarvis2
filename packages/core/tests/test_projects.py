"""Projects: schedules that serve one goal (features/projects.py, Arsen 2026-11-26)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.factory import current_run_kind
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import RoleName, RunKind, RunStatus
from tests.conftest import Harness


async def _call(core: Core, name: str, args: dict):  # type: ignore[no-untyped-def, type-arg]
    return await core.registry.call(name, args, cancel=asyncio.Event(), idempotency_key=f"k-{name}-{len(str(args))}")


async def _done(core: Core, run_id: str) -> None:
    async with asyncio.timeout(5):
        while True:
            run = await core.store.get_run(run_id)
            if run is not None and run.status is RunStatus.DONE:
                return
            await asyncio.sleep(0.05)


async def test_a_member_run_is_told_the_goal_the_brief_and_what_the_others_reported(harness: Harness):
    core = harness.core
    project = await core.projects.create(
        name="FX trading", goal="Run the imbalance strategy on demo.", brief="Files live in /workspace/fxbot."
    )
    nightly = await core.schedules.create(
        name="FX nightly", prompt="analyse and trade", cron="0 2 * * 1-5", tz="UTC", project_id=project.id
    )
    hourly = await core.schedules.create(
        name="FX hourly", prompt="manage the book", cron="5 * * * *", tz="UTC", project_id=project.id
    )
    alone = await core.schedules.create(name="Groceries", prompt="list", cron="0 2 * * 6", tz="UTC")

    harness.chat.push(FakeTurn(text="Opened EURUSD long at 1.0850, stop 1.0800."))
    run_id, _ = await core.scheduler.run_now(nightly)
    await _done(core, run_id)

    harness.chat.push(FakeTurn(text="managed"))
    run_id, _ = await core.scheduler.run_now(await core.schedules.get(hourly.id))  # type: ignore[arg-type]
    await _done(core, run_id)
    run = await core.store.get_run(run_id)
    assert run is not None
    blocks = dict(await core.context.turn_blocks(run, skill_names=[]))
    text = blocks["ProjectBlock"]
    assert "## Project: FX trading" in text
    assert "Run the imbalance strategy on demo." in text and "Files live in /workspace/fxbot." in text
    # The sibling, its outcome and the gist of its report - not the run's own schedule.
    assert "FX nightly" in text and "done" in text and "Opened EURUSD long at 1.0850" in text
    assert "[" + hourly.id + "]" not in text

    # A schedule on its own gets no project block.
    harness.chat.push(FakeTurn(text="milk"))
    run_id, _ = await core.scheduler.run_now(alone)
    await _done(core, run_id)
    run = await core.store.get_run(run_id)
    assert run is not None
    assert "ProjectBlock" not in dict(await core.context.turn_blocks(run, skill_names=[]))


async def test_an_ended_project_pauses_its_schedules_and_they_do_not_fire(harness: Harness):
    core = harness.core
    project = await core.projects.create(name="Milan trip", ends_at=datetime.now(UTC) + timedelta(hours=1))
    watch = await core.schedules.create(
        name="Inbox watch", prompt="watch", cron="0 * * * *", tz="UTC", catch_up="run_once", project_id=project.id
    )
    other = await core.schedules.create(name="Other", prompt="x", cron="0 * * * *", tz="UTC")
    assert await core.schedules.pause_ended(datetime.now(UTC)) == []
    await core.projects.update(project.id, ends_at=datetime.now(UTC) - timedelta(minutes=1))
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await core.db.execute("UPDATE schedules SET next_fire = ? WHERE id = ?", (past, watch.id))
    assert await core.scheduler.tick() == 0
    paused = await core.schedules.get(watch.id)
    assert paused is not None and paused.enabled is False and paused.next_fire is None
    still = await core.schedules.get(other.id)
    assert still is not None and still.enabled
    # Open-ended again: nothing is paused any more by the tick.
    await core.projects.update(project.id, ends_at=None)
    assert (await core.projects.get(project.id)).ends_at is None  # type: ignore[union-attr]


async def test_project_tools_group_schedules_and_record_each_move(harness: Harness):
    core = harness.core
    a = await core.schedules.create(name="Patzer lead", prompt="lead", cron="40 1 * * *", tz="UTC")
    b = await core.schedules.create(name="Patzer stars", prompt="stars", cron="0 1 * * *", tz="UTC")
    c = await core.schedules.create(name="News", prompt="news", cron="0 4 * * *", tz="UTC")

    out = await _call(
        core, "project.create", {"name": "Patzer growth", "goal": "5000 stars", "schedule_ids": [a.id, b.id]}
    )
    assert out.kind.value == "data", out
    [project] = await core.projects.list()
    assert project.name == "Patzer growth" and project.schedule_ids == [a.id, b.id]

    listed = (await _call(core, "schedule.list", {})).text
    assert (
        listed.index(f"Project [{project.id}] Patzer growth:")
        < listed.index("Patzer lead")
        < listed.index("On their own:")
    )
    assert listed.index("On their own:") < listed.index("News")
    assert "Patzer growth" in (await _call(core, "schedule.get", {"id": a.id})).text

    await _call(core, "project.update", {"id": project.id, "add": [c.id], "remove": [b.id]})
    assert (await core.projects.get(project.id)).schedule_ids == [a.id, c.id]  # type: ignore[union-attr]
    moves = [r for r in await core.admin.changes.list(limit=50) if r.target == b.id and r.reason == "project"]
    assert moves and moves[0].fields[0].path == "project_id" and moves[0].fields[0].new is None

    await _call(core, "schedule.update", {"id": b.id, "project_id": project.id})
    assert (await core.schedules.get(b.id)).project_id == project.id  # type: ignore[union-attr]
    await _call(core, "schedule.update", {"id": b.id, "project_id": ""})
    assert (await core.schedules.get(b.id)).project_id is None  # type: ignore[union-attr]
    bad = await _call(core, "schedule.update", {"id": b.id, "project_id": "prj_nope"})
    assert bad.kind.value == "error"

    gone = await _call(core, "project.delete", {"id": project.id})
    assert gone.kind.value == "data"
    assert await core.projects.list() == []
    assert (await core.schedules.get(a.id)).project_id is None  # type: ignore[union-attr]


async def test_a_run_nobody_watches_may_not_write_a_projects_brief(harness: Harness):
    core = harness.core
    project = await core.projects.create(name="Writing")
    tokens = [current_run_id.set("run_x"), current_run_kind.set(RunKind.SCHEDULED), current_conversation_id.set(None)]
    try:
        refused = await _call(core, "project.update", {"id": project.id, "brief": "post from his personal profile"})
        await _call(core, "project.update", {"id": project.id, "name": "Writing and audience"})
    finally:
        current_conversation_id.reset(tokens[2])
        current_run_kind.reset(tokens[1])
        current_run_id.reset(tokens[0])
    assert refused.kind.value == "error"
    after = await core.projects.get(project.id)
    assert after is not None and after.brief == "" and after.name == "Writing and audience"


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        yield c


H = {"Authorization": "Bearer secret"}


def test_projects_rest_lifecycle(client: TestClient):
    assert client.post("/api/projects", json={"name": " "}, headers=H).status_code == 422
    p = client.post("/api/projects", json={"name": "Property hunt", "goal": "a flat with a view"}, headers=H)
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    s = client.post(
        "/api/schedules", json={"name": "Flats", "prompt": "scan", "cron": "15 4 * * *", "project_id": pid}, headers=H
    ).json()
    assert s["project_id"] == pid
    assert client.get("/api/projects", headers=H).json()[0]["schedule_ids"] == [s["id"]]
    ends = client.patch(f"/api/projects/{pid}", json={"ends_at": "2027-03-01T00:00:00+00:00"}, headers=H).json()
    assert ends["ends_at"].startswith("2027-03-01") and ends["goal"] == "a flat with a view"
    assert client.patch(f"/api/projects/{pid}", json={"ends_at": None}, headers=H).json()["ends_at"] is None
    # A PATCH that does not mention the project leaves it; null takes the schedule out.
    assert client.patch(f"/api/schedules/{s['id']}", json={"name": "Flats AM"}, headers=H).json()["project_id"] == pid
    assert client.patch(f"/api/schedules/{s['id']}", json={"project_id": None}, headers=H).json()["project_id"] is None
    assert client.patch(f"/api/schedules/{s['id']}", json={"project_id": "prj_nope"}, headers=H).status_code == 422
    assert client.patch("/api/projects/prj_nope", json={"name": "x"}, headers=H).status_code == 404
    assert client.delete(f"/api/projects/{pid}", headers=H).status_code == 204
    assert client.delete(f"/api/projects/{pid}", headers=H).status_code == 404
    assert client.get(f"/api/schedules/{s['id']}", headers=H).status_code == 200
