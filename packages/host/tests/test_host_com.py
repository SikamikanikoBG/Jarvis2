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


def test_a_wedged_call_gets_the_next_caller_a_fresh_thread():
    # 2026-09-27: OUTLOOK.EXE sat on a modal "Sign in to set up Office" dialog; one list_items never
    # returned, the single COM thread stayed parked on it for 3.6 hours, and 65 calls queued behind it
    # all timed out until the daemon was restarted by hand.
    release = threading.Event()
    resets: list[int] = []
    w = ComWorker(init=None, wedge_after_s=0.3)
    w.on_respawn(lambda: resets.append(1))
    try:
        w.start()
        first = w.thread_ident
        stuck = w.submit(release.wait, label="outlook.list_items")
        time.sleep(0.5)  # longer than wedge_after_s: the worker is wedged
        assert w.call(threading.get_ident, timeout_s=2) != first  # served by a new thread, promptly
        assert resets == [1]  # the Outlook/OneNote connections were told to reconnect
        status = w.status()
        assert status.restarts == 1 and not status.busy  # the stuck call no longer counts as the current one
        release.set()  # the old call finally returns: its thread exits instead of competing for jobs
        assert stuck.result(timeout=2) is True
        assert w.call(threading.get_ident, timeout_s=2) == w.thread_ident
    finally:
        release.set()
        w.stop()


def test_a_slow_call_under_the_wedge_limit_keeps_its_thread():
    w = ComWorker(init=None, wedge_after_s=5)
    try:
        w.start()
        slow = w.submit(time.sleep, 0.3)
        assert w.call(threading.get_ident, timeout_s=2) == w.thread_ident
        slow.result(timeout=2)
        assert w.status().restarts == 0
    finally:
        w.stop()


def test_looking_at_the_status_also_replaces_a_wedged_thread():
    # 2026-09-28: host_status showed "busy for 655s on outlook.list_items" - the watchdog only ran on the
    # next submit, and host_status skips its ping while busy, so nothing ever submitted.
    release = threading.Event()
    w = ComWorker(init=None, wedge_after_s=0.3)
    try:
        w.start()
        w.submit(release.wait, label="outlook.list_items")
        time.sleep(0.5)
        status = w.status()
        assert status.restarts == 1 and not status.busy
    finally:
        release.set()
        w.stop()


def test_a_respawn_does_not_release_the_wedged_apartments_objects():
    # 2026-10-01: the respawn hook dropped the Outlook proxy on the event loop; the Release went to the
    # hung Outlook and blocked, freezing the daemon for 8 hours with its port still open.
    import gc
    import weakref

    from jarvis_host.onenote import OneNoteBackend
    from jarvis_host.outlook import OutlookBackend

    class Proxy:
        def GetNamespace(self, _name: str) -> Proxy:
            return Proxy()

    outlook = OutlookBackend(dispatch=Proxy)
    outlook._session()
    onenote = OneNoteBackend(dispatch=Proxy)
    onenote._session()
    held = [weakref.ref(outlook._app), weakref.ref(outlook._ns), weakref.ref(onenote._app)]
    outlook.abandon()
    onenote.abandon()
    gc.collect()
    assert outlook._ns is None and onenote._app is None  # the next call reconnects
    assert all(ref() is not None for ref in held)  # but nothing was released


def test_a_session_opened_by_another_thread_is_not_used():
    # 2026-10-02: a written-off thread finished connecting after the respawn and left its apartment's
    # namespace behind; the new thread used it and got RPC_E_WRONG_THREAD.
    from jarvis_host.outlook import OutlookBackend

    class Proxy:
        def GetNamespace(self, _name: str) -> Proxy:
            return Proxy()

    backend = OutlookBackend(dispatch=Proxy)
    other: list[object] = []
    t = threading.Thread(target=lambda: other.append(backend._session()))
    t.start()
    t.join()
    mine = backend._session()
    assert mine is not other[0]
    assert backend._session() is mine  # the own thread's session is kept
