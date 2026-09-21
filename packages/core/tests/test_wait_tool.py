"""The two waits. ``jarvis.wait`` is a blind pause that keeps the run alive and does not count as
time spent or as a loop (13 Sep 2026); ``jarvis.wait_until`` polls a read-only tool and comes back
the moment it says what was asked for, instead of a guessed number of seconds (21 Sep 2026)."""

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
from jarvis_proto import RoleName, ToolCall, ToolResult, ToolResultKind


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


# --- jarvis.wait_until: the same pause, but with something to check -----------------------


class _Probe(CoreTools):
    """A CoreTools wired to a fake registry whose one tool answers a scripted sequence."""

    def __init__(self, answers: list[ToolResult], *, read_only: bool = True) -> None:
        self.answers = answers
        self.calls: list[dict] = []
        self.read_only = read_only
        super().__init__(registry=lambda: self)  # type: ignore[arg-type]

    # -- the slice of ToolRegistry that wait_until uses
    def get(self, name: str):
        from jarvis_proto import ToolSpec

        if name != "probe.check":
            return None
        return ToolSpec(
            name=name,
            description="",
            input_schema={"type": "object", "properties": {}},
            read_only=self.read_only,
            provider="probe",
        )

    def validate(self, name: str, arguments: dict) -> str | None:
        return None

    def cannot_route(self, name: str) -> str:
        return f"unknown tool {name!r}"

    async def call(self, name: str, arguments: dict, *, cancel, idempotency_key: str, timeout_s: float):
        self.calls.append({"args": dict(arguments), "key": idempotency_key})
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


def test_wait_until_polls_until_the_result_matches_and_returns_it():
    """Three attempts: down, down, up. It comes back with the answer, not just a verdict."""
    probe = _Probe(
        [
            ToolResult.failure("connection refused"),
            ToolResult.data("Restarting (1) 2 seconds ago"),
            ToolResult.data("Up 3 seconds (healthy)"),
        ]
    )

    async def go():
        t0 = time.monotonic()
        res = await probe._wait_until(
            tool="probe.check",
            args={"command": "docker ps"},
            contains="Up ",
            timeout_s=30,
            interval_s=1,
            reason="the container",
            cancel=asyncio.Event(),
        )
        return res, time.monotonic() - t0

    res, took = asyncio.run(go())
    assert res.kind is ToolResultKind.DATA
    assert "3 attempts" in res.text and "the container" in res.text
    assert "Up 3 seconds (healthy)" in res.text, "the matching result rides back, so no second call"
    assert len(probe.calls) == 3 and probe.calls[0]["args"] == {"command": "docker ps"}
    assert len({c["key"] for c in probe.calls}) == 3, "each attempt gets its own idempotency key"
    assert 2.0 <= took < 6.0  # two intervals of 1 s, not three


def test_wait_until_with_no_pattern_waits_for_the_call_to_stop_failing():
    probe = _Probe([ToolResult.failure("connection refused"), ToolResult.data("pong")])
    res = asyncio.run(probe._wait_until(tool="probe.check", timeout_s=20, interval_s=1, cancel=asyncio.Event()))
    assert res.kind is ToolResultKind.DATA and "answered" in res.text and "pong" in res.text


def test_wait_until_gives_up_with_the_last_result_in_hand():
    probe = _Probe([ToolResult.data("Restarting (1)")])
    res = asyncio.run(
        probe._wait_until(tool="probe.check", contains="Up ", timeout_s=5, interval_s=1, cancel=asyncio.Event())
    )
    assert res.kind is ToolResultKind.ERROR
    assert "still not true" in res.text and "Restarting (1)" in res.text, "the model learns WHAT it saw"


def test_wait_until_refuses_a_tool_that_can_change_things():
    """Polling a mutation would send the mail once per attempt."""
    probe = _Probe([ToolResult.data("sent")], read_only=False)
    res = asyncio.run(probe._wait_until(tool="probe.check", timeout_s=5, cancel=asyncio.Event()))
    assert res.kind is ToolResultKind.ERROR and "read-only" in res.text and probe.calls == []


def test_wait_until_refuses_an_unknown_tool_and_itself():
    probe = _Probe([ToolResult.data("x")])
    unknown = asyncio.run(probe._wait_until(tool="nope.nope", cancel=asyncio.Event()))
    assert unknown.kind is ToolResultKind.ERROR and "unknown tool" in unknown.text
    recursive = asyncio.run(probe._wait_until(tool="jarvis.wait_until", cancel=asyncio.Event()))
    assert recursive.kind is ToolResultKind.ERROR and "itself a wait" in recursive.text
    assert probe.calls == []


def test_wait_until_ends_at_once_when_cancelled():
    probe = _Probe([ToolResult.data("not yet")])
    cancel = asyncio.Event()

    async def go():
        t0 = time.monotonic()
        task = asyncio.create_task(
            probe._wait_until(tool="probe.check", contains="ready", timeout_s=300, interval_s=30, cancel=cancel)
        )
        await asyncio.sleep(0.2)
        cancel.set()
        return await task, time.monotonic() - t0

    res, took = asyncio.run(go())
    assert res.kind is ToolResultKind.ERROR and "cancelled" in res.text
    assert took < 3.0, "it must not sit out the 30 s interval"


def test_a_bad_regex_is_still_matched_as_plain_text():
    probe = _Probe([ToolResult.data("progress (50%) done")])
    res = asyncio.run(probe._wait_until(tool="probe.check", contains="(50%)", timeout_s=5, cancel=asyncio.Event()))
    assert res.kind is ToolResultKind.DATA


def test_wait_until_is_clamped_and_its_time_is_credited_back(monkeypatch: pytest.MonkeyPatch):
    """A wait nobody bounded must not hang the run, and the seconds it burns are not work."""
    from jarvis_core.tools.builtin import IS_WAIT_TOOL, WAIT_UNTIL_MAX_S

    assert "jarvis.wait_until" in IS_WAIT_TOOL, "the loop credits its seconds back to the run clock"

    slept: list[float] = []
    cancel = asyncio.Event()

    async def fake_wait_for(coro, timeout):
        slept.append(timeout)
        coro.close()
        cancel.set()  # one interval is enough to see what it asked for
        raise TimeoutError

    monkeypatch.setattr("jarvis_core.tools.builtin.asyncio.wait_for", fake_wait_for)
    probe = _Probe([ToolResult.data("not yet")])
    res = asyncio.run(
        probe._wait_until(tool="probe.check", contains="ready", timeout_s=99_999, interval_s=9_999, cancel=cancel)
    )
    # Both numbers are clamped where they are used, so an unbounded ask cannot outlive the cap.
    assert slept == [300.0], "the interval is capped at 5 minutes"
    assert slept[0] <= float(WAIT_UNTIL_MAX_S)
    assert res.kind is ToolResultKind.ERROR and "cancelled" in res.text


def test_wait_until_registered_in_a_real_core_and_can_poll_a_real_tool(client: TestClient):
    """End to end against the live registry: it polls jarvis.time until the seconds match."""
    names = {t["name"] for t in client.get("/api/tools").json()}
    assert "jarvis.wait_until" in names
    core = client.core  # type: ignore[attr-defined]
    res = asyncio.run(
        core.registry.call(
            "jarvis.wait_until",
            {"tool": "jarvis.time", "args": {}, "contains": r"\d{4}-\d{2}-\d{2}", "timeout_s": 10, "interval_s": 1},
            cancel=asyncio.Event(),
            idempotency_key="k1",
        )
    )
    assert res.kind is ToolResultKind.DATA and "1 attempt," in res.text
