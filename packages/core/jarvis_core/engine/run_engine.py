"""RunEngine: the only way work gets to the model.

Queue by priority, bounded concurrency, real cancellation, resume after restart,
confirmation hand-off. Every state change is persisted before it is announced.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from jarvis_core.db import Store
from jarvis_core.engine.bus import EventBus
from jarvis_core.engine.control import RunCancelledError, RunControl
from jarvis_core.engine.emit import RunEmitter
from jarvis_core.engine.loop import AgentLoop
from jarvis_core.models.base import ModelError
from jarvis_proto import (
    Conversation,
    ConversationKind,
    Message,
    Role,
    Run,
    RunKind,
    RunStatus,
    Settings,
    ThinkLevel,
    new_id,
)
from jarvis_proto.events import (
    ConversationUpdated,
    MessageCreated,
    RunCancelled,
    RunFailed,
    RunInterrupted,
    RunQueued,
    RunUpdated,
    ToolConfirmResolved,
)

log = logging.getLogger(__name__)

_PRIORITY = {
    RunKind.CHAT: 0,
    RunKind.COLLAB: 1,
    RunKind.MEETING: 2,
    RunKind.SCHEDULED: 3,
    RunKind.TRIAGE: 4,
    RunKind.SYSTEM: 5,
}


class RunEngine:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        loop: AgentLoop,
        settings: Callable[[], Settings],
        *,
        max_concurrent: int = 3,
        titler: Callable[[str, str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._loop = loop
        self._settings = settings
        self._max = max_concurrent
        # Names a chat after its first completed exchange (docs/WAVE2.md slice 0); None = keep
        # the first-line title forever.
        self._titler = titler
        self._heap: list[tuple[int, str, str]] = []  # (priority, created_at iso, run_id)
        self._queued: dict[str, Run] = {}
        self._active: dict[str, tuple[asyncio.Task[None], RunControl]] = {}
        self._tasks: set[asyncio.Task[None]] = set()  # every run task until it is truly finished
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._stopping = False

    def set_titler(self, titler: Callable[[str, str], Awaitable[str | None]] | None) -> None:
        """Swap or disable the auto-titler (tests script every model turn and switch it off)."""
        self._titler = titler

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        await self._resume_on_boot()
        self._dispatcher = asyncio.create_task(self._dispatch(), name="run-dispatcher")

    async def stop(self) -> None:
        """Graceful shutdown: running runs are marked interrupted so the next boot resumes them."""
        self._stopping = True
        if self._dispatcher:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
        for run_id, (task, _ctl) in list(self._active.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            log.info("run %s interrupted by shutdown", run_id)
        # Tasks in their final bookkeeping must finish before the DB closes under them.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _resume_on_boot(self) -> None:
        for run in await self._store.runs_with_status(RunStatus.RUNNING, RunStatus.CANCELLING):
            emitter = RunEmitter(run, self._store, self._bus)
            run.status = RunStatus.INTERRUPTED
            await self._store.save_run(run)
            await emitter.emit(RunInterrupted(run_id="", conversation_id=""))
            self._enqueue(run, resumed=True)
        for run in await self._store.runs_with_status(RunStatus.QUEUED, RunStatus.INTERRUPTED):
            if run.id not in self._queued:
                self._enqueue(run, resumed=run.status is RunStatus.INTERRUPTED)
        # waiting_user runs stay parked until confirm() arrives.

    # --- public API ----------------------------------------------------------------

    async def create_run(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        kind: RunKind = RunKind.CHAT,
        conversation_kind: ConversationKind | None = None,
        folder_key: str | None = None,
        folder_label: str | None = None,
        title: str | None = None,
        think: bool | None = None,
        think_level: ThinkLevel | None = None,
        budget_kind: RunKind | None = None,
    ) -> tuple[Run, Conversation]:
        conv: Conversation | None = None
        if conversation_id:
            conv = await self._store.get_conversation(conversation_id)
        if conv is None:
            conv = await self._store.create_conversation(
                kind=conversation_kind or ConversationKind(kind.value if kind is not RunKind.SYSTEM else "chat"),
                title=title or _title_from(text),
                folder_key=folder_key,
                folder_label=folder_label,
            )
            self._bus.publish(ConversationUpdated(conversation=conv))
        elif conv.title == "New chat" and conv.message_count == 0:
            conv = await self._store.update_conversation(conv.id, title=_title_from(text)) or conv
            self._bus.publish(ConversationUpdated(conversation=conv))

        user_msg = await self._store.add_message(Message.user(text, conversation_id=conv.id))
        self._bus.publish(MessageCreated(message=user_msg))

        settings = self._settings()
        run = Run(
            id=new_id("run"),
            conversation_id=conv.id,
            kind=kind,
            input_text=text,
            budget=settings.budgets.get(budget_kind or kind, Run(id="", conversation_id="", kind=kind).budget),
            priority=_PRIORITY[kind],
            think=think,
            think_level=think_level,
        )
        await self._store.create_run(run)
        # run_id must be set on the user message so resume can find the run's messages.
        await self._store.db.execute("UPDATE messages SET run_id = ? WHERE id = ?", (run.id, user_msg.id))
        emitter = RunEmitter(run, self._store, self._bus)
        await emitter.emit(
            RunQueued(run_id="", conversation_id="", kind=kind, input_preview=text[:120], user_message_id=user_msg.id)
        )
        await self._store.save_run(run)
        self._enqueue(run)
        conv = await self._store.get_conversation(conv.id) or conv
        self._bus.publish(ConversationUpdated(conversation=conv))
        return run, conv

    async def cancel(self, run_id: str) -> bool:
        if run_id in self._queued:
            run = self._queued.pop(run_id)
            self._heap = [h for h in self._heap if h[2] != run_id]
            heapq.heapify(self._heap)
            run.status = RunStatus.CANCELLED
            run.finished_at = datetime.now(UTC)
            await self._store.save_run(run)
            await RunEmitter(run, self._store, self._bus).emit(RunCancelled(run_id="", conversation_id=""))
            return True
        entry = self._active.get(run_id)
        if entry is None:
            run = await self._store.get_run(run_id)
            if run is not None and run.status is RunStatus.WAITING_USER:
                run.status = RunStatus.CANCELLED
                run.finished_at = datetime.now(UTC)
                await self._store.save_run(run)
                await RunEmitter(run, self._store, self._bus).emit(RunCancelled(run_id="", conversation_id=""))
                return True
            return False
        _task, ctl = entry
        ctl.emitter.run.status = RunStatus.CANCELLING
        await self._store.save_run(ctl.emitter.run)
        self._bus.publish(RunUpdated(run=ctl.emitter.run.model_copy()))
        ctl.cancel.set()
        return True

    async def confirm(self, run_id: str, call_id: str, approved: bool, note: str | None = None) -> bool:
        entry = self._active.get(run_id)
        if entry is not None and entry[1].resolve_confirmation(call_id, approved, note):
            return True
        # Not in memory: the core restarted while waiting. Record the decision, re-queue.
        run = await self._store.get_run(run_id)
        if run is None or run.status is not RunStatus.WAITING_USER:
            return False
        emitter = RunEmitter(run, self._store, self._bus)
        await emitter.emit(
            ToolConfirmResolved(run_id="", conversation_id="", call_id=call_id, approved=approved, note=note)
        )
        await self._store.save_run(run)
        self._enqueue(run, resumed=True)
        return True

    def active_run_ids(self) -> list[str]:
        return list(self._active)

    def queued_count(self) -> int:
        return len(self._queued)

    # --- internals -----------------------------------------------------------------

    def _enqueue(self, run: Run, *, resumed: bool = False) -> None:
        if run.status not in {RunStatus.QUEUED, RunStatus.INTERRUPTED, RunStatus.WAITING_USER}:
            run.status = RunStatus.QUEUED
        self._queued[run.id] = run
        setattr(run, "_resumed", resumed)  # noqa: B010 - transient flag, not part of the model
        heapq.heappush(self._heap, (run.priority, run.created_at.isoformat(), run.id))
        self._wake.set()

    async def _dispatch(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._heap and self._running_count() < self._max and not self._stopping:
                _p, _t, run_id = heapq.heappop(self._heap)
                run = self._queued.pop(run_id, None)
                if run is None:
                    continue
                resumed = bool(getattr(run, "_resumed", False))
                ctl = RunControl(emitter=RunEmitter(run, self._store, self._bus), resumed=resumed)
                task = asyncio.create_task(self._execute(run, ctl), name=f"run-{run.id}")
                self._active[run.id] = (task, ctl)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _execute(self, run: Run, ctl: RunControl) -> None:
        emitter = ctl.emitter
        try:
            await self._loop.run(run, ctl)
        except RunCancelledError as exc:
            run.status = RunStatus.CANCELLED
            run.finished_at = datetime.now(UTC)
            await self._store.save_run(run)
            await emitter.emit(RunCancelled(run_id="", conversation_id="", partial_message_id=exc.partial_message_id))
        except asyncio.CancelledError:
            # Shutdown. A parked run stays parked; anything else becomes resumable.
            if run.status is not RunStatus.WAITING_USER:
                run.status = RunStatus.INTERRUPTED
                await self._store.save_run(run)
                await emitter.emit(RunInterrupted(run_id="", conversation_id=""))
            raise
        except ModelError as exc:
            await self._fail(run, emitter, f"model: {exc}")
        except Exception as exc:
            log.error("run %s crashed: %s\n%s", run.id, exc, traceback.format_exc())
            await self._fail(run, emitter, f"{type(exc).__name__}: {exc}")
        finally:
            self._active.pop(run.id, None)
            await self._after_run(run)
            self._wake.set()

    def _running_count(self) -> int:
        """Parked (waiting_user) runs hold no slot: a question to Arsen must not block the queue."""
        return sum(1 for _t, c in self._active.values() if c.emitter.run.status is not RunStatus.WAITING_USER)

    async def _fail(self, run: Run, emitter: RunEmitter, error: str) -> None:
        run.status = RunStatus.FAILED
        run.error = error
        run.finished_at = datetime.now(UTC)
        await self._store.save_run(run)
        await emitter.emit(RunFailed(run_id="", conversation_id="", error=error))

    async def _after_run(self, run: Run) -> None:
        unread = run.status.terminal and not self._bus.has_subscribers(run.conversation_id)
        conv = (
            await self._store.update_conversation(run.conversation_id, unread=int(unread))
            if unread
            else await self._store.get_conversation(run.conversation_id)
        )
        if conv is not None:
            self._bus.publish(ConversationUpdated(conversation=conv))
        self._bus.publish(RunUpdated(run=run.model_copy()))
        if (
            conv is not None
            and self._titler is not None
            and run.status is RunStatus.DONE
            and conv.title_auto
            and conv.kind is ConversationKind.CHAT
        ):
            await self._maybe_title(run, conv)

    async def _maybe_title(self, run: Run, conv: Conversation) -> None:
        """Name the chat once, after its FIRST completed exchange; later turns keep the name."""
        done = [r for r in await self._store.list_runs(conv.id) if r.status is RunStatus.DONE]
        if len(done) != 1:
            return
        msgs = await self._store.list_run_messages(run.id)
        user = next((m.content for m in msgs if m.role is Role.USER and not m.name), run.input_text)
        reply = next((m.content for m in reversed(msgs) if m.role is Role.ASSISTANT and m.content), "")
        if not reply:
            return
        assert self._titler is not None
        try:
            title = await self._titler(user, reply)
        except Exception:
            log.exception("titler failed")
            return
        if title and title != conv.title:
            updated = await self._store.update_conversation(conv.id, title=title)
            if updated:
                self._bus.publish(ConversationUpdated(conversation=updated))


def _title_from(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "New chat"
    return (line[:57] + "…") if len(line) > 60 else line
