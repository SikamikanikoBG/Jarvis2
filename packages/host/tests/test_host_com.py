"""The COM worker: one thread, per-call deadlines, no zombie waiters."""

from __future__ import annotations

import threading
import time

import pytest

from jarvis_host.com import ComTimeout, ComWorker


@pytest.fixture
def worker():
    w = ComWorker(init=None, name="test")
    w.start()
    yield w
    w.stop()


def test_every_call_runs_on_the_same_dedicated_thread(worker: ComWorker):
    first = worker.call(threading.get_ident, timeout_s=5)
    second = worker.call(threading.get_ident, timeout_s=5)
    assert first == second == worker.thread_ident
    assert first != threading.get_ident()


def test_exceptions_belong_to_the_caller(worker: ComWorker):
    def boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        worker.call(boom, timeout_s=5)
    assert worker.call(lambda: 42, timeout_s=5) == 42  # the worker survived


def test_timeout_returns_to_the_caller_while_the_worker_finishes(worker: ComWorker):
    finished = threading.Event()

    def slow() -> str:
        time.sleep(0.6)
        finished.set()
        return "late"

    t0 = time.monotonic()
    with pytest.raises(ComTimeout, match=r"slow did not finish within 0\.15s"):
        worker.call(slow, timeout_s=0.15)
    assert time.monotonic() - t0 < 0.5, "the caller must not wait for the slow call"
    status = worker.status()
    assert status.busy and status.current == "slow" and status.abandoned == 1
    assert finished.wait(2), "the worker keeps running the abandoned call to completion"
    time.sleep(0.05)
    assert worker.call(lambda: "next", timeout_s=5) == "next"
    status = worker.status()
    assert not status.busy and status.abandoned == 1 and status.completed >= 2


def test_a_call_that_times_out_in_the_queue_is_cancelled_not_run(worker: ComWorker):
    ran = threading.Event()
    release = threading.Event()

    def blocker() -> None:
        release.wait(3)

    def never() -> None:
        ran.set()

    worker.submit(blocker)
    with pytest.raises(ComTimeout):
        worker.call(never, timeout_s=0.1)
    release.set()
    time.sleep(0.2)
    assert not ran.is_set(), "a job nobody is waiting for must not execute"
    assert worker.status().abandoned == 0, "cancelled-before-start is not an abandoned (zombie) call"
    assert worker.call(lambda: 1, timeout_s=5) == 1


async def test_acall_has_the_same_semantics(worker: ComWorker):
    assert await worker.acall(lambda: "ok", timeout_s=5) == "ok"
    done = threading.Event()

    def slow() -> None:
        time.sleep(0.4)
        done.set()

    with pytest.raises(ComTimeout):
        await worker.acall(slow, timeout_s=0.1)
    assert worker.status().busy
    assert done.wait(2)
    assert await worker.acall(lambda: "after", timeout_s=5) == "after"


def test_status_when_idle(worker: ComWorker):
    s = worker.status()
    assert s.alive and not s.busy and s.pending == 0 and s.current is None
