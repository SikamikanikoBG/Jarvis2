"""The single COM worker thread.

Outlook's COM objects belong to the apartment of the thread that created them, so every COM
call goes through one dedicated thread that ``CoInitialize``s once and lives for the process.
Callers submit a function and wait on a future with a deadline. When the deadline passes the
caller gets ``ComTimeout`` and walks away; the worker is *not* interrupted (there is no safe way
to abort a COM call mid-flight) — it finishes the slow call, drops the result, and moves on. A
job that has not started yet is simply cancelled. The result is that a timeout never leaves a
thread parked on a lock: V1 accumulated 20 such zombies in 8 minutes with ``wait_for(to_thread)``.

``busy`` / ``abandoned`` are exposed so ``host_status`` can say "Outlook is still chewing on the
last call" instead of the next caller discovering it by timing out too.

"Finishes the slow call" assumed Outlook eventually answers. It does not when it sits on a modal
dialog ("Sign in to set up Office", 2026-09-27): one call stayed in flight for 3.6 hours and every
call after it timed out until the daemon was restarted. So a call busy longer than ``wedge_after_s``
counts as wedged: the next caller gets a fresh COM thread (the queued jobs move with it, and the
``on_respawn`` hooks drop the Outlook/OneNote objects so they reconnect from the new apartment). The
wedged thread is written off; if its call ever returns, it exits instead of taking more work.

The hooks must not *release* those objects: they run on the event loop, and dropping the last
reference to a proxy of a hung Outlook is a cross-apartment Release that blocks until Outlook
answers. That froze the whole daemon for 9 minutes (2026-10-01 07:41) and for 8 hours (22:05 until
it was killed the next morning) — the port stayed open, but core saw only connect timeouts. So the
old objects go to ``write_off``, which keeps them alive for the life of the process.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_TIMEOUT_S = 30.0
# Longer than any per-call deadline (the slowest Outlook op allows 120 s): a call past this is not slow, it is stuck.
WEDGE_AFTER_S = 180.0


_written_off: list[Any] = []


def write_off(*objs: Any) -> None:
    """Keep COM objects of a wedged apartment alive forever: releasing them can block on a hung app."""
    _written_off.extend(o for o in objs if o is not None)


class ComTimeout(TimeoutError):
    """The COM call did not finish within its deadline. The worker keeps running it."""


@dataclass(frozen=True)
class ComStatus:
    alive: bool
    busy: bool
    busy_for_s: float
    current: str | None
    pending: int
    abandoned: int
    completed: int
    restarts: int = 0


class _Job:
    __slots__ = ("args", "fn", "future", "kwargs", "label")

    def __init__(self, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], label: str) -> None:
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.label = label
        self.future: Future[Any] = Future()


def _default_init() -> Callable[[], None] | None:
    try:
        import pythoncom  # pyright: ignore[reportMissingModuleSource]
    except ImportError:
        return None
    return pythoncom.CoInitialize


class ComWorker:
    def __init__(
        self,
        *,
        init: Callable[[], None] | None = None,
        name: str = "com",
        wedge_after_s: float | None = WEDGE_AFTER_S,
    ) -> None:
        self._init = init if init is not None else _default_init()
        self._name = name
        self._wedge_after_s = wedge_after_s
        self._on_respawn: list[Callable[[], None]] = []
        self._restarts = 0
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current: _Job | None = None
        self._current_since = 0.0
        self._abandoned = 0
        self._completed = 0
        self._stopped = False

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopped = False
            self._spawn()

    def _spawn(self) -> None:
        """Start a thread on the current queue. Caller holds the lock."""
        self._thread = threading.Thread(
            target=self._run, args=(self._queue,), name=f"jarvis-host-{self._name}", daemon=True
        )
        self._thread.start()

    def on_respawn(self, hook: Callable[[], None]) -> None:
        """Called after a wedged thread is replaced: drop COM objects that belong to the old apartment."""
        self._on_respawn.append(hook)

    def _respawn_if_wedged(self) -> None:
        if self._wedge_after_s is None:
            return
        with self._lock:
            cur = self._current
            if cur is None or self._stopped or time.monotonic() - self._current_since < self._wedge_after_s:
                return
            old = self._queue
            self._queue = queue.SimpleQueue()
            while True:  # the waiting jobs move to the new thread
                try:
                    job = old.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    self._queue.put(job)
            old.put(None)  # the wedged thread exits if its call ever returns
            self._current = None
            self._restarts += 1
            self._spawn()
            stuck_for = time.monotonic() - self._current_since
        log.error(
            "com: %s wedged for %.0fs; replaced the COM thread (restart %d)", cur.label, stuck_for, self._restarts
        )
        for hook in self._on_respawn:
            try:
                hook()
            except Exception as exc:
                log.warning("com: respawn hook failed: %s", exc)

    def stop(self, *, wait_s: float = 2.0) -> None:
        with self._lock:
            self._stopped = True
            thread = self._thread
        with self._lock:
            jobs = self._queue
        jobs.put(None)
        if thread is not None:
            thread.join(wait_s)

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread is not None else None

    def status(self) -> ComStatus:
        # Checked here too: host_status reads this and skips its own call while busy, so a wedge
        # seen only by status readers would never be replaced (655 s on 2026-09-28).
        self._respawn_if_wedged()
        with self._lock:
            cur = self._current
            since = self._current_since
            return ComStatus(
                alive=self._thread is not None and self._thread.is_alive(),
                busy=cur is not None,
                busy_for_s=round(time.monotonic() - since, 3) if cur is not None else 0.0,
                current=cur.label if cur is not None else None,
                pending=self._queue.qsize(),
                abandoned=self._abandoned,
                completed=self._completed,
                restarts=self._restarts,
            )

    # --- submission ----------------------------------------------------------------------

    def submit(self, fn: Callable[..., T], *args: Any, label: str | None = None, **kwargs: Any) -> Future[T]:
        if self._thread is None or not self._thread.is_alive():
            self.start()
        self._respawn_if_wedged()
        job = _Job(fn, args, kwargs, label or getattr(fn, "__name__", "call"))
        self._queue.put(job)
        return job.future

    def call(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        label: str | None = None,
        **kwargs: Any,
    ) -> T:
        """Run ``fn`` on the COM thread and wait at most ``timeout_s`` for the result."""
        fut = self.submit(fn, *args, label=label, **kwargs)
        try:
            return fut.result(timeout=timeout_s)
        except FutureTimeout:
            self._abandon(fut, label or getattr(fn, "__name__", "call"), timeout_s)
            raise ComTimeout(
                f"{label or getattr(fn, '__name__', 'call')} did not finish within {timeout_s:g}s"
            ) from None

    async def acall(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        label: str | None = None,
        **kwargs: Any,
    ) -> T:
        """``call`` for asyncio callers — no helper thread is parked while waiting."""
        fut = self.submit(fn, *args, label=label, **kwargs)
        name = label or getattr(fn, "__name__", "call")
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout_s)
        except TimeoutError:
            self._abandon(fut, name, timeout_s)
            raise ComTimeout(f"{name} did not finish within {timeout_s:g}s") from None
        except CancelledError:
            self._abandon(fut, name, timeout_s)
            raise

    def _abandon(self, fut: Future[Any], name: str, timeout_s: float) -> None:
        if fut.cancel():
            log.warning(
                "com: %s cancelled before it started (waited %.0fs behind %s)", name, timeout_s, self.status().current
            )
            return
        with self._lock:
            self._abandoned += 1
        log.warning("com: %s abandoned after %.0fs; the worker will finish it in the background", name, timeout_s)

    # --- the thread -----------------------------------------------------------------------

    def _run(self, jobs: queue.SimpleQueue[_Job | None]) -> None:
        if self._init is not None:
            try:
                self._init()
            except Exception as exc:  # pragma: no cover — only on a broken COM runtime
                log.error("com: CoInitialize failed: %s", exc)
        while True:
            job = jobs.get()
            if job is None or self._stopped:
                break
            if not job.future.set_running_or_notify_cancel():
                continue  # cancelled by a caller that gave up before we got here
            with self._lock:
                self._current = job
                self._current_since = time.monotonic()
            error: BaseException | None = None
            result: Any = None
            try:
                result = job.fn(*job.args, **job.kwargs)
            except BaseException as exc:
                error = exc
            # Book-keeping BEFORE the future is resolved: whoever wakes up on the result must
            # not be able to observe a status snapshot that still calls this job in flight.
            with self._lock:
                if self._current is job:  # a written-off (wedged) thread no longer owns the status
                    self._current = None
                self._completed += 1
            if error is not None:
                job.future.set_exception(error)
            else:
                job.future.set_result(result)
