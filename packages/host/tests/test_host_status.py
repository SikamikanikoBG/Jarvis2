"""host_status — a hung Outlook is diagnosed, not waited for."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_com import World

from jarvis_host.com import ComWorker
from jarvis_host.config import HostConfig
from jarvis_host.outlook import OutlookBackend, OutlookService
from jarvis_host.status import Status


@pytest.fixture
def worker() -> Iterator[ComWorker]:
    w = ComWorker(init=None)
    w.start()
    yield w
    w.stop()


def make_status(world: World, worker: ComWorker, process_state) -> Status:
    cfg = HostConfig(name="t", token="x", fs_roots=(Path.home(),))
    return Status(cfg, worker, OutlookService(OutlookBackend(world.dispatch), worker), process_state=process_state)


async def test_healthy_outlook_is_pinged(worker: ComWorker):
    world = World()
    status = make_status(world, worker, lambda: {"running": True, "hung": False, "processes": [{"pid": 1}]})
    snap = await status.snapshot()
    ol = snap["outlook"]
    assert ol["connected"] is True and ol["accounts"] == ["aapostolov@postbank.bg", "arsen@gmail.com"]
    assert ol["process"]["running"] is True and "diagnosis" not in ol
    assert world.app.dispatch_count == 1
    assert snap["com"]["completed"] == 1 and snap["name"] == "t" and snap["uptime_s"] >= 0


async def test_hung_outlook_is_diagnosed_without_touching_com(worker: ComWorker):
    world = World()
    status = make_status(
        world, worker, lambda: {"running": True, "hung": True, "processes": [{"pid": 7708, "hung": True}]}
    )
    t0 = time.monotonic()
    snap = await status.snapshot()
    assert time.monotonic() - t0 < 1.0
    ol = snap["outlook"]
    assert ol["connected"] is None and "not responding" in ol["error"] and "hung" in ol["diagnosis"]
    assert world.app.dispatch_count == 0, "no COM call may be queued against a hung Outlook"
    assert not snap["com"]["busy"]


async def test_missing_outlook_process(worker: ComWorker):
    world = World()
    status = make_status(world, worker, lambda: {"running": False, "hung": False, "processes": []})
    ol = (await status.snapshot())["outlook"]
    assert ol["diagnosis"] == "OUTLOOK.EXE is not running"
    assert ol["connected"] is True, "the fake dispatch still answers; the diagnosis is informational"


async def test_busy_worker_skips_the_ping(worker: ComWorker):
    world = World()
    status = make_status(world, worker, lambda: None)
    release = __import__("threading").Event()
    worker.submit(release.wait, 5, label="outlook.list_items")
    await asyncio.sleep(0.05)  # let the worker thread pick the job up
    snap = await status.snapshot()
    release.set()
    ol = snap["outlook"]
    assert ol["connected"] is None and "busy" in ol["error"] and "outlook.list_items" in ol["error"]
    assert snap["com"]["busy"] is True and snap["com"]["current"] == "outlook.list_items"
    assert "process" not in ol  # process_state returned None (non-Windows path)
