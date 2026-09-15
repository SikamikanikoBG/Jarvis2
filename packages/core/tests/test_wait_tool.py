"""jarvis.wait: a general-purpose pause that keeps the run alive and does not count as time spent
or as a loop. The GPAI answer to "wait for the build, then carry on" (13 Sep 2026)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.supervision import RunBudget, RunWatch
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_core.tools.builtin import WAIT_MAX_S, CoreTools
from jarvis_proto import RoleName, ToolCall, ToolResultKind


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=None))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.fake = fake  # type: ignore[attr-defined]
        c.core = core  # type: ignore[attr-defined]
        yield c


def test_wait_sleeps_the_requested_time_and_reports_back():
    async def go():
        t0 = time.monotonic()
        res = await CoreTools()._wait(1, reason="the build", cancel=asyncio.Event())
        return res, time.monotonic() - t0

    res, took = asyncio.run(go())
    assert res.kind is ToolResultKind.DATA
    assert "Waited" in res.text and "waiting for the build" in res.text
    assert 0.9 <= took < 2.0  # the 1 s floor; sub-second waits are pointless


def test_wait_ends_at_once_when_cancelled():
    async def go():
        cancel = asyncio.Event()
        task = asyncio.create_task(CoreTools()._wait(30, cancel=cancel))
        await asyncio.sleep(0.05)
        cancel.set()
        t0 = time.monotonic()
        res = await task
        return res, time.monotonic() - t0

    res, took = asyncio.run(go())
    assert res.kind is ToolResultKind.DATA and "interrupted" in res.text
    assert took < 1.0  # did not sit out the 30 s


def test_wait_is_clamped_to_the_cap(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []

    async def fake_wait_for(coro, timeout):
        slept.append(timeout)
        coro.close()
        raise TimeoutError

    monkeypatch.setattr("jarvis_core.tools.builtin.asyncio.wait_for", fake_wait_for)
    asyncio.run(CoreTools()._wait(999999, cancel=asyncio.Event()))
    assert slept == [float(WAIT_MAX_S)]


def test_wait_does_not_spend_the_time_budget():
    """The whole point: a run that waited 300 s of a 600 s budget has not spent 300 s of work."""
    watch = RunWatch(budget=RunBudget(max_seconds=600))
    watch.started = time.monotonic() - 550  # 550 s of wall clock have passed
    watch.paused_s = 500  # ...of which 500 were a deliberate wait
    assert watch.elapsed_s() < 60
    watch.paused_s = 0
    assert watch.elapsed_s() >= 550


def test_wait_tool_registered_and_runs_end_to_end_in_a_chat(client: TestClient):
    names = {t["name"] for t in client.get("/api/tools").json()}
    assert "jarvis.wait" in names
    client.fake.push(  # type: ignore[attr-defined]
        FakeTurn(
            tool_calls=[ToolCall(id="c1", name="jarvis.wait", arguments={"seconds": 0.1, "reason": "the deploy"})]
        ),
        FakeTurn(text="deploy done, carrying on"),
    )
    import json

    client.core.settings.planning_enabled = False  # type: ignore[attr-defined]
    with client.websocket_connect("/ws") as ui:
        ui.send_text(json.dumps({"type": "run.create", "text": "wait for the deploy then check"}))
        result_text = None
        while True:
            ev = json.loads(ui.receive_text())
            if ev["type"] == "tool.result" and ev["name"] == "jarvis.wait":
                result_text = ev["result"]["text"]
            if ev["type"] in {"run.done", "run.failed"}:
                assert ev["type"] == "run.done"
                break
    assert result_text is not None and "Waited" in result_text and "the deploy" in result_text


def test_the_core_gives_a_wait_its_full_duration_not_the_default_cap():
    """A 400 s wait must not be killed at the 120 s default tool timeout."""
    from types import SimpleNamespace

    from jarvis_core.engine.loop import _TOOL_TIMEOUT_S, AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._settings = lambda: SimpleNamespace(tool_timeout_max_s=1200)  # type: ignore[attr-defined]
    long_wait = ToolCall(id="c1", name="jarvis.wait", arguments={"seconds": 400})
    assert loop._tool_timeout(long_wait) == 430.0  # 400 + 30 slack, NOT min(120, 1200)
    other = ToolCall(id="c2", name="some.tool", arguments={})
    assert loop._tool_timeout(other) == _TOOL_TIMEOUT_S
