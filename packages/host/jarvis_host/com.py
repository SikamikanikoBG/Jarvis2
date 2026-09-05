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
    def __init__(self, *, init: Callable[[], None] | None = None, name: str = "com") -> None:
        self._init = init if init is not None else _default_init()
        self._name = name
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
            self._thread = threading.Thread(target=self._run, name=f"jarvis-host-{self._name}", daemon=True)
            self._thread.start()

    def stop(self, *, wait_s: float = 2.0) -> None:
        with self._lock:
            self._stopped = True
            thread = self._thread
        self._queue.put(None)
        if thread is not None:
            thread.join(wait_s)

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread is not None else None

    def status(self) -> ComStatus:
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
            )

    # --- submission ----------------------------------------------------------------------

    def submit(self, fn: Callable[..., T], *args: Any, label: str | None = None, **kwargs: Any) -> Future[T]:
        if self._thread is None or not self._thread.is_alive():
            self.start()
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

    def _run(self) -> None:
        if self._init is not None:
            try:
                self._init()
            except Exception as exc:  # pragma: no cover — only on a broken COM runtime
                log.error("com: CoInitialize failed: %s", exc)
        while True:
            job = self._queue.get()
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
                self._current = None
                self._completed += 1
            if error is not None:
                job.future.set_exception(error)
            else:
                job.future.set_result(result)
