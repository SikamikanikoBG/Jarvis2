"""Delegated tasks: "on it, I'll come back to you" (docs/stories/13_delegated_tasks.md).

Until 2026-10-29 every request was a run Arsen watched until it ended, and on a call he sat on
the line while Jarvis worked. A real assistant takes the job, says so in a sentence, hands it to
someone with the context and a Definition of Done, and comes back when it is done - or says
plainly that it is not, and why. That is what this module adds, on top of the engine as it is:

- **A task is a run of its own, in a conversation of its own** (``RunKind.TASK``,
  ``ConversationKind.TASK``). Not a run inside the chat that asked: the engine does not serialise
  runs per conversation, so a second run there would interleave with Arsen's next turn and churn
  that chat's cached prefix. The chat that asked shows a card and, at the end, a report.
- **Only tasks Jarvis announced to him.** ``tasks.delegate`` is offered only to a run Arsen's own
  message started (``Run.from_owner``) and refused in a run another session has joined; a task
  cannot delegate further. The 2026-09-19 rule - Jarvis does not strike up conversations with
  itself - holds for everything else (features/sessions.py).
- **Nothing outward without his OK.** A task is not "unattended" in the scheduled-run sense: every
  action that is not read-only waits for his approval, whatever ``confirmations`` says
  (settings.tasks; engine/loop.py). Shell and file writes are not offered to a task at all.
- **"Done" is checked, not claimed.** A task ends with ``task.finish``: per DoD item, met or not,
  with the ids of the calls or files that show it. The code looks each one up in the task's own
  log; an item without valid evidence is "claimed, not verified", and such a task is never done.
- **Honest about "can I".** What is down right now rides in a chat's context (``HealthBlock``);
  the tools a task needs are checked when it is made and again when it starts, and a task whose
  tool's machine goes down mid-way ends on it ("not done: X down since 08:10").

- **On a call, note it; do it after.** A call turn notes longer work (``tasks.note``: a title,
  his words kept by the code) and stays a conversation. The call's page sends its id with every
  turn, a heartbeat, and ``call.ended`` at hang-up; a page that went quiet for
  ``tasks.call_drop_after_s`` has dropped. Either way the noted tasks start exactly once (one
  conditional UPDATE), each writes its own brief as its first step, and one after-call report
  says how each went.

The knowing-enough and understanding-the-goal halves of "can I" are the model's judgement, led
by the tool descriptions. There is no list of words anywhere in here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import Subscriber
from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.features.callme import clip, plain, sentences
from jarvis_core.features.planner import PLAN_TOOLS
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import (
    Channel,
    ConversationKind,
    DodItem,
    DodResult,
    Message,
    Role,
    Run,
    RunKind,
    RunStatus,
    Settings,
    Task,
    TaskBrief,
    TaskOutcome,
    TaskState,
    TaskWaiting,
    ToolResult,
    ToolSpec,
    new_id,
)
from jarvis_proto.events import (
    ConversationUpdated,
    MessageCreated,
    RunCancelled,
    RunDone,
    RunFailed,
    RunResumed,
    RunStarted,
    RunWaitingUser,
    TaskChanged,
    TaskRemoved,
    ToolConfirmRequested,
    ToolConfirmResolved,
)
from jarvis_proto.settings import CALL_WORK_NOW, VOICE_STYLE_DEFAULT

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

#: How often the sweeper looks at approvals nobody answered.
SWEEP_S = 30.0
#: What a report quotes of a final answer when the task never wrote its own summary.
REPORT_ANSWER_CHARS = 1_200
#: The tools that write a task down from a chat; only Arsen's own message may call them.
_DELEGATING = frozenset({"tasks.delegate", "tasks.note"})
#: Never offered to a task: it does not message other chats, and it does not ring Arsen - its
#: report does (one quiet notice when it ends).
_NOT_FOR_TASKS = frozenset({"sessions.say", "jarvis.call_me"})
#: A task's own tools that work before its brief is written.
_BEFORE_THE_BRIEF = frozenset({"task.brief", "task.origin", "task.ask"})
#: Bookkeeping, not evidence: a DoD item cannot be shown by the task's own notes.
_NOT_EVIDENCE_PREFIXES = ("task.", "tasks.")
_PLAN_NAMES = frozenset(spec.name for spec in PLAN_TOOLS)

_STATE_WORDS = {
    TaskState.NOTED: "noted",
    TaskState.QUEUED: "queued",
    TaskState.RUNNING: "working",
    TaskState.WAITING: "waiting for your approval",
    TaskState.NEEDS_INPUT: "needs your answer",
    TaskState.DONE: "done",
    TaskState.PARTIAL: "partly done",
    TaskState.NOT_DONE: "not done",
    TaskState.FAILED: "failed",
    TaskState.CANCELLED: "cancelled",
    TaskState.DROPPED: "dropped",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# --- the table ---------------------------------------------------------------------------------


class TaskStore:
    """Raw SQL over ``tasks`` (migration 0019), the ScheduleStore way."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _task(row: Any) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            state=TaskState(row["state"]),
            brief=TaskBrief.model_validate_json(row["brief"]),
            origin_conversation_id=row["origin_conversation_id"],
            origin_run_id=row["origin_run_id"],
            call_id=row["call_id"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            depth=row["depth"],
            outcome=TaskOutcome.model_validate_json(row["outcome"]) if row["outcome"] else None,
            waiting=TaskWaiting.model_validate_json(row["waiting"]) if row["waiting"] else None,
            report_message_id=row["report_message_id"],
            incognito=bool(row["incognito"]),
            note=row["note"],
            created_at=_dt(row["created_at"]) or _now(),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            updated_at=_dt(row["updated_at"]) or _now(),
        )

    async def create(self, task: Task, *, origin_key: str) -> Task:
        """Insert, or return the task this key already made: a resumed run that calls
        tasks.delegate again for the same tool call gets the first task back, not a second."""
        await self.db.execute(
            "INSERT OR IGNORE INTO tasks(id, origin_conversation_id, origin_run_id, origin_key, call_id,"
            " conversation_id, run_id, depth, title, brief, state, outcome, waiting, report_message_id,"
            " incognito, note, created_at, started_at, finished_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,NULL,?,NULL,NULL,?)",
            (
                task.id,
                task.origin_conversation_id,
                task.origin_run_id,
                origin_key,
                task.call_id,
                task.conversation_id,
                task.run_id,
                task.depth,
                task.title,
                task.brief.model_dump_json(),
                task.state.value,
                int(task.incognito),
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )
        row = await self.db.fetchone("SELECT * FROM tasks WHERE origin_key = ?", (origin_key,))
        assert row is not None
        return self._task(row)

    async def get(self, task_id: str) -> Task | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._task(row) if row else None

    async def by_run(self, run_id: str) -> Task | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE run_id = ?", (run_id,))
        return self._task(row) if row else None

    async def by_conversation(self, conversation_id: str) -> Task | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE conversation_id = ?", (conversation_id,))
        return self._task(row) if row else None

    async def list(
        self,
        *,
        origin: str | None = None,
        open_only: bool = False,
        call_id: str | None = None,
        states: tuple[TaskState, ...] = (),
        limit: int = 200,
    ) -> list[Task]:
        where: list[str] = []
        params: list[Any] = []
        if origin is not None:
            where.append("origin_conversation_id = ?")
            params.append(origin)
        if call_id is not None:
            where.append("call_id = ?")
            params.append(call_id)
        if open_only:
            states = tuple(s for s in TaskState if not s.terminal)
        if states:
            where.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(s.value for s in states)
        sql = "SELECT * FROM tasks" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._task(r) for r in await self.db.fetchall(sql, params)]

    async def save(self, task: Task) -> Task:
        task.updated_at = _now()
        await self.db.execute(
            "UPDATE tasks SET title=?, brief=?, state=?, conversation_id=?, run_id=?, outcome=?, waiting=?,"
            " report_message_id=?, note=?, started_at=?, finished_at=?, updated_at=? WHERE id=?",
            (
                task.title,
                task.brief.model_dump_json(),
                task.state.value,
                task.conversation_id,
                task.run_id,
                task.outcome.model_dump_json() if task.outcome else None,
                task.waiting.model_dump_json() if task.waiting else None,
                task.report_message_id,
                task.note,
                task.started_at.isoformat() if task.started_at else None,
                task.finished_at.isoformat() if task.finished_at else None,
                task.updated_at.isoformat(),
                task.id,
            ),
        )
        return task

    async def claim_report(self, task_id: str, message_id: str) -> bool:
        """The right to write this round's report, exactly once: whoever sets the id first."""
        rows = await self.db.fetchall(
            "UPDATE tasks SET report_message_id = ? WHERE id = ? AND report_message_id IS NULL RETURNING id",
            (message_id, task_id),
        )
        return bool(rows)

    async def runs_of_call(self, call_id: str) -> list[str]:
        rows = await self.db.fetchall("SELECT id FROM runs WHERE call_id = ? ORDER BY created_at", (call_id,))
        return [r["id"] for r in rows]

    async def claim_noted(self, call_id: str) -> list[str]:
        """Every task noted on this call, moved to queued in one statement on the one writer: a
        second hang-up, a reconnect or the sweeper finds nothing left to start."""
        rows = await self.db.fetchall(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE call_id = ? AND state = ? RETURNING id",
            (TaskState.QUEUED.value, _now().isoformat(), call_id, TaskState.NOTED.value),
        )
        return [r["id"] for r in rows]


# --- evidence -----------------------------------------------------------------------------------


class _Ledger:
    """What the task's own conversation shows happened: every call, its result, its approval."""

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.approved: set[str] = set()
        self.files: set[str] = set()

    def why_not(self, ref: str, *, outward: bool) -> str | None:
        """None when ``ref`` shows the item; otherwise why not, in a few words."""
        if ref in self.files:
            return f"{ref} is a file, not an action on anyone's behalf" if outward else None
        result = self.results.get(ref)
        if result is None:
            return f"{ref} is not a call or a file of this task"
        name = str(result.get("name") or "")
        if name.startswith(_NOT_EVIDENCE_PREFIXES) or name in _PLAN_NAMES:
            return f"{ref} is the task's own bookkeeping ({name})"
        if (result.get("result") or {}).get("kind") == "error":
            return f"{ref} ({name}) failed"
        if not outward:
            return None
        call = self.calls.get(ref) or {}
        args = call.get("arguments") or {}
        if call.get("read_only", True):
            return f"{ref} ({name}) only read"
        if ref not in self.approved:
            return f"{ref} ({name}) was not approved by Arsen"
        if args.get("draft") or "_policy_note" in args:
            return f"{ref} ({name}) was saved as a draft, it did not go out"
        return None


async def ledger_of(core: Core, task: Task) -> _Ledger:
    ledger = _Ledger()
    if task.conversation_id is None:
        return ledger
    for run in await core.store.list_runs(task.conversation_id, limit=200):
        for ev in await core.store.list_events(run.id):
            kind, call_id = ev.get("type"), ev.get("call_id")
            if not call_id:
                continue
            if kind == "tool.call":
                ledger.calls[call_id] = ev
            elif kind == "tool.result":
                ledger.results[call_id] = ev
            elif kind == "tool.confirm_resolved" and ev.get("approved"):
                ledger.approved.add(call_id)
    ledger.files = {a.id for a in await core.attachments.for_conversation(task.conversation_id)}
    return ledger


def judge(brief: TaskBrief, claimed: list[DodResult], ledger: _Ledger) -> list[DodResult]:
    """Every DoD item of the brief, with what the task claimed and what the code found.

    Matched by position, not by wording: ``claimed[i]`` answers ``brief.dod[i]`` (task.finish
    takes the item's number). An item the task never answered is "not reported"."""
    out: list[DodResult] = []
    for i, item in enumerate(brief.dod):
        said = claimed[i] if i < len(claimed) else None
        result = DodResult(item=item.item, outward=item.outward)
        if said is not None:
            result.met, result.refs, result.note = said.met, list(said.refs), said.note
        if result.met is True:
            reasons = [why for ref in result.refs if (why := ledger.why_not(ref, outward=item.outward))]
            good = [ref for ref in result.refs if ledger.why_not(ref, outward=item.outward) is None]
            result.verified = bool(good)
            if not result.verified:
                result.why = "; ".join(reasons) if reasons else "no evidence given"
        out.append(result)
    return out


def state_of(results: list[DodResult]) -> TaskState:
    """Done only when every item is verified; anything met or claimed is partly done."""
    if results and all(r.verified for r in results):
        return TaskState.DONE
    if any(r.met is True for r in results):
        return TaskState.PARTIAL
    return TaskState.NOT_DONE


# --- what the chat that asked reads -------------------------------------------------------------


def _dod_line(r: DodResult) -> str:
    if r.met is True and r.verified:
        return f"- ✅ {r.item}"
    if r.met is True:
        return f"- ⚠️ {r.item} — claimed, not verified ({r.why})"
    if r.met is False:
        return f"- ❌ {r.item}" + (f" — {r.note}" if r.note else "")
    return f"- ◻️ {r.item} — not reported"


def render_report(task: Task, *, answer: str = "") -> str:
    """The report in the chat that asked, in English, built by code from the task's data: what
    it says about each DoD item is what the code verified, not what the model wrote."""
    outcome = task.outcome or TaskOutcome()
    head = f"**Task: {task.title} — {_STATE_WORDS[task.state]}**"
    parts = [head]
    summary = outcome.summary.strip()
    if not summary and answer.strip():
        summary = answer.strip()
        if len(summary) > REPORT_ANSWER_CHARS:
            summary = summary[:REPORT_ANSWER_CHARS].rsplit(" ", 1)[0] + " … (the rest is in the task's own log)"
    if summary:
        parts.append(summary)
    if outcome.dod:
        parts.append("\n".join(_dod_line(r) for r in outcome.dod))
    if outcome.stopped_reason:
        parts.append(f"Stopped: {outcome.stopped_reason}")
    return "\n\n".join(parts)


def brief_text(task: Task, *, user: str, call: str = "") -> str:
    """The task's first message: the brief as the task's model reads it. ``call``: the call it
    was noted on, as it went, for a task whose brief it writes itself."""
    b = task.brief
    lines = [f"# Task: {task.title}"]
    if not b.written:
        lines.append(
            "This task was noted on a call. Its brief is not written yet: write it first, with task.brief, "
            "from his words and the call below."
        )
    if b.goal:
        lines.append(f"## Goal\n{b.goal}")
    if b.asked:
        said = "\n".join(f"> {line}" for text in b.asked for line in text.splitlines())
        lines.append(f"## {user}'s own words (as he said them)\n{said}")
    if b.context:
        lines.append(f"## Context\n{b.context}")
    if b.constraints:
        lines.append("## Constraints\n" + "\n".join(f"- {c}" for c in b.constraints))
    if b.dod:
        dod = "\n".join(
            f"{i}. {d.item}" + (" (outward: done only by an approved action)" if d.outward else "")
            for i, d in enumerate(b.dod, 1)
        )
        lines.append("## Definition of Done\n" + dod)
    if b.approvals:
        lines.append("## Needs his approval\n" + "\n".join(f"- {a}" for a in b.approvals))
    if b.assumptions:
        lines.append("## Assumed (he did not say)\n" + "\n".join(f"- {a}" for a in b.assumptions))
    if call:
        lines.append(f"## The call it was noted on\n{call}")
    return "\n\n".join(lines)


def _one_line(text: str, limit: int) -> str:
    return clip(" ".join(str(text).split()), limit)


# --- the service ---------------------------------------------------------------------------------


class TaskService:
    """Glue on the bus, the CallMe way: it keeps every task's row in step with its run, writes
    the report into the chat that asked, pushes one quiet notice, and answers the loop's
    questions (``engine.loop.TaskGate``)."""

    def __init__(self, core: Core, *, sweep_s: float = SWEEP_S) -> None:
        self.core = core
        self.store = TaskStore(core.db)
        self.sweep_s = sweep_s
        self._sub: Subscriber | None = None
        self._task: asyncio.Task[None] | None = None
        self._sweeper: asyncio.Task[None] | None = None
        #: Task ids whose report waits for a run in the chat that asked to end.
        self._deferred: set[str] = set()
        #: Task ids held between steps while a call is open (shown on the card, never stored).
        self._paused: set[str] = set()
        #: (task id, tool call id) already pushed as "needs your approval".
        self._asked: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        #: Open calls: call id -> when the page last said it was there (the calls table, in memory).
        self._calls: dict[str, datetime] = {}
        #: Set at boot when the stored call style could not be brought up to date (Settings → Voice).
        self.voice_style_note: str | None = None

    @property
    def settings(self) -> Settings:
        return self.core.settings

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self._sub = Subscriber("tasks", everything=True)
        self.core.bus.attach(self._sub)
        self._task = asyncio.create_task(self._consume(self._sub), name="tasks-events")
        self._sweeper = asyncio.create_task(self._sweep_forever(), name="tasks-sweeper")

    async def stop(self) -> None:
        if self._sub is not None:
            self.core.bus.detach(self._sub)
            self._sub = None
        for task in (self._task, self._sweeper):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._sweeper = None

    async def reconcile(self) -> None:
        """After a restart: nothing is lost silently. A round whose run ended while the core was
        down is finished now; a task whose run is gone is reported as failed; a report that was
        owed is written; calls that were open are open again (the sweeper ends the dropped ones),
        and what was noted on a call that had already ended starts."""
        for row in await self.core.db.fetchall("SELECT id, last_seen_at FROM calls WHERE ended_at IS NULL"):
            self._calls[row["id"]] = _dt(row["last_seen_at"]) or _now()
        for row in await self.core.db.fetchall(
            "SELECT DISTINCT t.call_id FROM tasks t JOIN calls c ON c.id = t.call_id"
            " WHERE t.state = ? AND c.ended_at IS NOT NULL",
            (TaskState.NOTED.value,),
        ):
            await self._start_noted(row["call_id"])
        for task in await self.store.list(open_only=True):
            if task.state in (TaskState.NOTED, TaskState.NEEDS_INPUT) or task.run_id is None:
                continue
            run = await self.core.store.get_run(task.run_id)
            if run is None:
                task.outcome = (task.outcome or TaskOutcome()).model_copy(
                    update={"stopped_reason": "its run was lost in a restart"}
                )
                await self._end(task, TaskState.FAILED)
            elif run.status.terminal:
                await self._round_ended(task, run)
            elif run.status is RunStatus.WAITING_USER:
                await self._mirror_waiting(task, run)
        for task in await self.store.list(states=tuple(s for s in TaskState if s.terminal), limit=50):
            if self._owes_report(task):
                await self._report(task)

    async def _mirror_waiting(self, task: Task, run: Run) -> None:
        """The card's approval, rebuilt from the run's own log: the question it is parked on is the
        last one asked and not yet answered ("did it complete?" after a restart included)."""
        asked: dict[str, Any] = {}
        for ev in await self.core.store.list_events(run.id):
            if ev.get("type") == "tool.confirm_requested":
                asked[str(ev.get("call_id"))] = ev
            elif ev.get("type") == "tool.confirm_resolved":
                asked.pop(str(ev.get("call_id")), None)
        if not asked:
            return
        ev = list(asked.values())[-1]
        task.waiting = TaskWaiting(
            run_id=run.id,
            call_id=str(ev.get("call_id")),
            name=str(ev.get("name")),
            arguments=dict(ev.get("arguments") or {}),
            reason=str(ev.get("reason") or ""),
        )
        task.state = TaskState.WAITING
        if any(e.get("type") == "run.resumed" for e in await self.core.store.list_events(run.id)):
            task.note = "resumed after a restart of the core"
        await self.store.save(task)
        self.publish(task)

    # --- the loop's questions (engine.loop.TaskGate) -----------------------------------------

    def hidden(self, run: Run, spec: ToolSpec) -> bool:
        return self._rule(run, spec.name, steered_by_other=False) is not None

    async def refusal(self, run: Run, name: str, *, steered_by_other: bool) -> str | None:
        if (why := self._rule(run, name, steered_by_other=steered_by_other)) is not None:
            return why
        if run.kind is RunKind.TASK and name not in _BEFORE_THE_BRIEF:
            task = await self.store.by_run(run.id)
            if task is not None and not task.brief.written:
                return "write the brief first with task.brief - the work starts from it"
        return None

    def _rule(self, run: Run, name: str, *, steered_by_other: bool) -> str | None:
        """Why ``name`` is not for this run, or None. The same rule hides it from the list and
        refuses it when named anyway."""
        s = self.settings.tasks
        namespace = name.split(".", 1)[0]
        mine = namespace in ("tasks", "task")
        if mine and not s.enabled:
            return "delegated tasks are switched off (settings.tasks.enabled)"
        if run.kind is RunKind.TASK:
            if namespace == "tasks":
                return f"a task cannot hand work on to another task (settings.tasks.max_depth={s.max_depth})"
            if s.denies(name) or name in _NOT_FOR_TASKS:
                return f"{name} is not available to a delegated task; do it another way or say in the report that it needs Arsen"
            return None
        if namespace == "task":
            return f"{name} belongs to a delegated task's own run"
        if namespace != "tasks":
            return None
        if run.kind is not RunKind.CHAT or not run.from_owner or steered_by_other:
            return (
                "a task can only be started from Arsen's own message in a chat; tell him what you would hand "
                "off and let him ask for it"
            )
        if name in _DELEGATING:
            on_a_call = run.channel is Channel.VOICE and run.call_id is not None
            if name == "tasks.note" and not on_a_call:
                return "tasks.note is for a call; in a chat use tasks.delegate"
            if name == "tasks.delegate" and on_a_call:
                return "on a call, note it with tasks.note; it starts when the call ends"
        elif name in ("tasks.drop", "tasks.amend") and not (run.channel is Channel.VOICE and run.call_id):
            return f"{name} is for a task noted on this call"
        return None

    async def unreachable(self, run: Run, reason: str) -> None:
        task = await self.store.by_run(run.id)
        if task is None:
            return
        task.outcome = (task.outcome or TaskOutcome()).model_copy(update={"stopped_reason": reason})
        await self.store.save(task)

    def calls_open(self) -> int:
        return len(self._calls)

    def call_open(self) -> bool:
        """Arsen is on a call: a call turn is running, or a call's page is still there. A task
        neither starts nor takes a step while this is true - the voice comes first."""
        return self.core.engine.on_a_call() or bool(self._calls)

    async def paused(self, run: Run, on: bool) -> None:
        task = await self.store.by_run(run.id)
        if task is None:
            return
        (self._paused.add if on else self._paused.discard)(task.id)
        self.publish(task)

    def may_start(self, running: int) -> bool:
        """The engine's question for a queued task run (``RunEngine.set_task_gate``)."""
        return running < self.settings.tasks.max_running and not self.call_open()

    # --- making and starting tasks ------------------------------------------------------------

    async def asked_words(self, run_id: str) -> list[str]:
        """Arsen's own words in this run: what started it and what he added while it worked,
        as stored - never retyped by a model."""
        return [
            m.content
            for m in await self.core.store.list_run_messages(run_id)
            if m.role is Role.USER and m.name is None and m.content.strip()
        ]

    async def create(
        self, *, run: Run, origin_key: str, title: str, brief: TaskBrief, state: TaskState
    ) -> tuple[Task, bool]:
        """A new task from the run that asked; (task, made now). False = this tool call had
        already made it (a resumed run)."""
        origin = await self.core.store.get_conversation(run.conversation_id)
        task = Task(
            id=new_id("task"),
            title=title.strip()[:80] or "Task",
            state=state,
            brief=brief,
            origin_conversation_id=run.conversation_id,
            origin_run_id=run.id,
            call_id=run.call_id if state is TaskState.NOTED else None,
            incognito=bool(origin is not None and origin.incognito),
        )
        stored = await self.store.create(task, origin_key=origin_key)
        made = stored.id == task.id
        if made:
            log.info("task %s made (%s) from run %s: %s", stored.id, stored.state.value, run.id, stored.title)
            self.publish(stored)
        return stored, made

    def down_for(self, needs: list[str]) -> tuple[str, str, datetime] | None:
        """The first namespace the task needs whose machine is down: (namespace, error, since)."""
        for namespace in needs:
            if (down := self.core.registry.down(namespace)) is not None:
                return namespace, down[0], down[1]
        return None

    def clock(self, at: datetime) -> str:
        return at.astimezone(ZoneInfo(self.settings.timezone)).strftime("%H:%M")

    async def begin(self, task: Task, text: str | None = None) -> Task:
        """Give the task its conversation and a run, or end it honestly when a machine it needs is
        down - checked now, not only when it was made: a task noted on a call starts minutes later.

        Under the lock the run's events are handled with, so its first event finds the task
        already pointing at it."""
        async with self._lock:
            return await self._start(task, text)

    async def _start(self, task: Task, text: str | None) -> Task:
        if (down := self.down_for(task.brief.needs)) is not None:
            namespace, error, since = down
            task.outcome = (task.outcome or TaskOutcome()).model_copy(
                update={"stopped_reason": f"not started: {namespace} is down since {self.clock(since)} ({error})"}
            )
            await self._end(task, TaskState.NOT_DONE)
            return task
        if task.conversation_id is None:
            origin = await self.core.store.get_conversation(task.origin_conversation_id)
            conv = await self.core.store.create_conversation(
                kind=ConversationKind.TASK,
                title=task.title,
                folder_key=task.origin_conversation_id,
                folder_label=origin.title if origin else None,
                incognito=task.incognito,
            )
            await self.core.store.update_conversation(conv.id, title_auto=0)
            task.conversation_id = conv.id
            self.core.bus.publish(ConversationUpdated(conversation=conv))
        user = self.settings.user_name
        if text is None:
            call = await self.call_transcript(task) if task.call_id and not task.brief.written else ""
            text = brief_text(task, user=user, call=call)
        run, _ = await self.core.engine.create_run(
            text=text,
            conversation_id=task.conversation_id,
            kind=RunKind.TASK,
            call_me=False,
        )
        task.run_id = run.id
        task.state = TaskState.QUEUED
        task.started_at = task.started_at or _now()
        task.finished_at = None
        task.report_message_id = None
        task.note = None
        task.waiting = None
        if task.outcome is not None:
            task.outcome = task.outcome.model_copy(update={"question": None, "stopped_reason": None})
        await self.store.save(task)
        self.publish(task)
        return task

    async def call_transcript(self, task: Task) -> str:
        """What was said on the call a task was noted on, copied by the code: his words and the
        replies, in order - what its first step writes the brief from."""
        if not task.call_id:
            return ""
        user = self.settings.user_name
        lines: list[str] = []
        for run_id in await self.store.runs_of_call(task.call_id):
            for m in await self.core.store.list_run_messages(run_id):
                if not m.content.strip() or m.name is not None:
                    continue
                if m.role is Role.USER:
                    lines.append(f"{user}: {m.content.strip()}")
                elif m.role is Role.ASSISTANT and not m.tool_calls:
                    lines.append(f"Jarvis: {m.content.strip()}")
        return "\n".join(lines)

    # --- calls ---------------------------------------------------------------------------------

    async def call_seen(self, call_id: str, conversation_id: str | None) -> None:
        """The call's page is there: a turn, or its heartbeat. A call that had been taken for
        dropped and speaks again is open again - only what is noted from now on waits for its end."""
        if not self.settings.tasks.enabled or not call_id:
            return
        now = _now().isoformat()
        await self.core.db.execute(
            "INSERT INTO calls(id, conversation_id, started_at, last_seen_at) VALUES (?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at, ended_at = NULL, end_reason = NULL,"
            " conversation_id = COALESCE(calls.conversation_id, excluded.conversation_id)",
            (call_id, conversation_id, now, now),
        )
        self._calls[call_id] = _now()

    async def call_ended(self, call_id: str, *, how: str) -> list[str]:
        """Hang-up or drop: the call is over and what was noted on it starts, exactly once - a
        second hang-up, a reconnect or the sweeper finds nothing left to start."""
        self._calls.pop(call_id, None)
        rows = await self.core.db.fetchall(
            "UPDATE calls SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL RETURNING id",
            (_now().isoformat(), how, call_id),
        )
        if rows:
            log.info("call %s ended (%s)", call_id, how)
        started = await self._start_noted(call_id)
        self.core.engine.wake()
        return started

    async def _start_noted(self, call_id: str) -> list[str]:
        started: list[str] = []
        for task_id in await self.store.claim_noted(call_id):
            task = await self.store.get(task_id)
            if task is not None:
                await self.begin(task)
                started.append(task_id)
        return started

    async def _sweep_calls(self, at: datetime) -> None:
        """A call whose page has said nothing for ``call_drop_after_s`` has dropped - never one
        that is only quiet (the page keeps saying it is there), and never in the middle of a turn."""
        limit = timedelta(seconds=self.settings.tasks.call_drop_after_s)
        for call_id, last in list(self._calls.items()):
            if at - last >= limit and not self.core.engine.call_turn_running(call_id):
                await self.call_ended(call_id, how="drop")

    def queued_ahead(self, task: Task, tasks: list[Task]) -> int:
        return sum(1 for t in tasks if t.id != task.id and t.state in (TaskState.QUEUED, TaskState.RUNNING))

    # --- steering from the chat or the card ----------------------------------------------------

    async def cancel(self, task_id: str) -> Task | None:
        task = await self.store.get(task_id)
        if task is None or task.state.terminal:
            return task
        if task.state is TaskState.NOTED:
            task.note = "dropped before it started"
            await self._end(task, TaskState.DROPPED)
            return task
        run = await self.core.store.get_run(task.run_id) if task.run_id else None
        if run is not None and not run.status.terminal:
            await self.core.engine.cancel(run.id)  # the run's end finishes the task (RunCancelled)
            return task
        task.outcome = (task.outcome or TaskOutcome()).model_copy(update={"stopped_reason": "cancelled by Arsen"})
        await self._end(task, TaskState.CANCELLED)
        return task

    async def steer(self, task_id: str, text: str) -> Task | None:
        """His words for a task: into the working run, or a new round of it when it has stopped
        (an answer to its question, or a change of mind after the report)."""
        task = await self.store.get(task_id)
        words = text.strip()
        if task is None or not words or task.state is TaskState.DROPPED:
            return task
        if task.state is TaskState.NOTED:
            task.brief.asked.append(words)
            await self.store.save(task)
            self.publish(task)
            return task
        if task.run_id and self.core.engine.steer(task.run_id, words):
            return task
        return await self.begin(task, text=words)

    # --- the run's events -----------------------------------------------------------------------

    async def _consume(self, sub: Subscriber) -> None:
        while True:
            event = await sub.queue.get()
            try:
                await self.on_event(event)
            except Exception:
                log.exception("tasks: %s not handled", getattr(event, "type", type(event).__name__))

    async def on_event(self, event: Any) -> None:
        if not isinstance(
            event,
            RunStarted
            | RunResumed
            | ToolConfirmRequested
            | RunWaitingUser
            | ToolConfirmResolved
            | RunDone
            | RunFailed
            | RunCancelled,
        ):
            return
        task = await self.store.by_run(event.run_id)
        if task is None:
            return
        async with self._lock:
            task = await self.store.get(task.id) or task
            if isinstance(event, RunStarted | RunResumed):
                if task.state in (TaskState.QUEUED, TaskState.RUNNING):
                    task.state = TaskState.RUNNING
                    if isinstance(event, RunResumed):
                        task.note = "resumed after a restart of the core"
                    await self.store.save(task)
                    self.publish(task)
            elif isinstance(event, ToolConfirmRequested):
                task.waiting = TaskWaiting(
                    run_id=event.run_id,
                    call_id=event.call_id,
                    name=event.name,
                    arguments=dict(event.arguments),
                    reason=event.reason,
                )
                await self.store.save(task)
            elif isinstance(event, RunWaitingUser):
                task.state = TaskState.WAITING
                await self.store.save(task)
                self.publish(task)
                if (task.id, event.call_id or "") not in self._asked:
                    self._asked.add((task.id, event.call_id or ""))
                    await self._push_waiting(task)
                if task.call_id:
                    await self._settle_call(task)
            elif isinstance(event, ToolConfirmResolved):
                task.waiting = None
                if task.state is TaskState.WAITING:
                    task.state = TaskState.RUNNING
                await self.store.save(task)
                self.publish(task)
            else:
                run = await self.core.store.get_run(event.run_id)
                if run is not None:
                    await self._round_ended(task, run, summary=getattr(event, "summary", None))

    async def _round_ended(self, task: Task, run: Run, *, summary: str | None = None) -> None:
        """The task's run ended: what state that leaves the task in, decided from what happened."""
        if task.state.terminal or run.id != task.run_id:
            return
        outcome = task.outcome or TaskOutcome()
        task.waiting = None
        self._paused.discard(task.id)
        if run.status is RunStatus.CANCELLED:
            outcome.stopped_reason = outcome.stopped_reason or "cancelled by Arsen"
            task.outcome = outcome
            await self._end(task, TaskState.CANCELLED)
            return
        if run.status is RunStatus.FAILED:
            outcome.stopped_reason = run.error or "the run failed"
            task.outcome = outcome
            await self._end(task, TaskState.FAILED)
            return
        if outcome.question and not outcome.finished:
            task.state = TaskState.NEEDS_INPUT
            task.outcome = outcome
            await self.store.save(task)
            self.publish(task)
            await self._push(task, f"Task needs your answer · {task.title}", outcome.question)
            if task.call_id:
                await self._settle_call(task)
            return
        ledger = await ledger_of(self.core, task)
        if outcome.finished:
            outcome.dod = judge(task.brief, outcome.dod, ledger)
            state = state_of(outcome.dod)
        else:
            # It ended without saying how it went: the budget, the supervisor, a machine that
            # went down. Nothing is claimed, so nothing is done.
            outcome.dod = judge(task.brief, [], ledger)
            outcome.stopped_reason = outcome.stopped_reason or summary or "it ended without reporting on its DoD"
            state = TaskState.NOT_DONE
        task.outcome = outcome
        await self._end(task, state)

    async def _end(self, task: Task, state: TaskState) -> None:
        task.state = state
        task.finished_at = _now()
        task.waiting = None
        await self.store.save(task)
        log.info("task %s → %s", task.id, state.value)
        self.publish(task)
        if self._owes_report(task):
            await self._report(task)
        self.core.engine.wake()

    @staticmethod
    def _owes_report(task: Task) -> bool:
        return task.state.terminal and task.state is not TaskState.DROPPED and task.report_message_id is None

    # --- the report -------------------------------------------------------------------------------

    async def after_run(self, run: Run) -> None:
        """A run ended anywhere (``RunEngine.on_finished``): reports that waited for that chat to
        fall quiet go in now."""
        if not self._deferred:
            return
        for task_id in list(self._deferred):
            task = await self.store.get(task_id)
            if task is None or task.origin_conversation_id != run.conversation_id:
                continue
            self._deferred.discard(task_id)
            if task.state.terminal:
                await self._report(task)
            elif task.call_id and task.state.settled:
                await self._settle_call(task)

    async def _settle_call(self, task: Task) -> None:
        """A task from a call now waits on Arsen: if it was the last of the call's to settle, the
        after-call report goes out now rather than when the last approval is answered."""
        if self.core.engine.working_run_in(task.origin_conversation_id):
            self._deferred.add(task.id)
            return
        await self._call_report(task)

    async def _final_answer(self, task: Task) -> Message | None:
        if task.run_id is None:
            return None
        msgs = await self.core.store.list_run_messages(task.run_id)
        return next(
            (m for m in reversed(msgs) if m.role is Role.ASSISTANT and not m.tool_calls and m.content.strip()), None
        )

    async def _report(self, task: Task) -> None:
        """Into the chat that asked, once per round, and only while no run is working there - a
        message written in the middle of a run would land in that run's history. Tasks from one
        call report together, once the last of them has settled."""
        if not self._owes_report(task):
            return
        if self.core.engine.working_run_in(task.origin_conversation_id):
            self._deferred.add(task.id)
            return
        if await self.core.store.get_conversation(task.origin_conversation_id) is None:
            return  # the chat is gone, and with it the place to report
        if task.call_id and await self._call_report(task):
            return
        answer = await self._final_answer(task)
        report = Message.assistant(
            render_report(task, answer=answer.content if answer else ""),
            id=new_id("msg"),
            conversation_id=task.origin_conversation_id,
            task_id=task.id,
        )
        assert report.id is not None
        if not await self.store.claim_report(task.id, report.id):
            return
        task.report_message_id = report.id
        user = self.settings.user_name
        # The model reads the history as turns: a report with no question before it reads as
        # Jarvis talking to himself, and an unnamed one as Arsen. Named and worded as the core's.
        note = Message.user(
            f'[core note, not from {user}] The delegated task "{task.title}" is {_STATE_WORDS[task.state]}; '
            "its report follows.",
            name="task",
            conversation_id=task.origin_conversation_id,
            task_id=task.id,
        )
        # The files it attached travel with the report, to where he asked.
        await self._post(task.origin_conversation_id, note, report, [task])
        self.publish(task)
        outcome = task.outcome or TaskOutcome()
        body = outcome.summary or (answer.content if answer else "") or outcome.stopped_reason or ""
        await self._push(task, f"Task {_STATE_WORDS[task.state]} · {task.title}", body)

    async def _call_report(self, task: Task) -> bool:
        """The after-call report: every task noted on one call, in one message, with one notice -
        written when the last of them has ended or is waiting on Arsen. False when the call's
        report has already gone out (this task finished after it, and reports on its own)."""
        call_id = task.call_id or ""
        row = await self.core.db.fetchone("SELECT report_message_id FROM calls WHERE id = ?", (call_id,))
        if row is not None and row["report_message_id"]:
            return False
        tasks = [t for t in await self.store.list(call_id=call_id) if t.state is not TaskState.DROPPED]
        tasks.sort(key=lambda t: t.created_at)
        if any(not t.state.settled for t in tasks):
            return True  # the rest are still going; the last one to settle writes it
        report = Message.assistant("", id=new_id("msg"), conversation_id=task.origin_conversation_id, task_id=task.id)
        assert report.id is not None
        claimed = await self.core.db.fetchall(
            "UPDATE calls SET report_message_id = ? WHERE id = ? AND report_message_id IS NULL RETURNING id",
            (report.id, call_id),
        )
        if row is not None and not claimed:
            return True  # someone else is writing it
        ended = [t for t in tasks if t.state.terminal]
        for t in ended:
            if await self.store.claim_report(t.id, report.id):
                t.report_message_id = report.id
        sections: list[str] = []
        for t in tasks:
            if t.state.terminal:
                answer = await self._final_answer(t)
                sections.append(render_report(t, answer=answer.content if answer else ""))
            else:
                sections.append(f"**Task: {t.title} — {_STATE_WORDS[t.state]}**\n{_waiting_line(t)}")
        counts: dict[str, int] = {}
        for t in tasks:
            counts[_STATE_WORDS[t.state]] = counts.get(_STATE_WORDS[t.state], 0) + 1
        tally = ", ".join(f"{n} {words}" for words, n in counts.items())
        report.content = f"**After the call — {tally}**\n\n" + "\n\n".join(sections)
        user = self.settings.user_name
        note = Message.user(
            f"[core note, not from {user}] The tasks noted on the call are through ({tally}); the after-call "
            "report follows.",
            name="task",
            conversation_id=task.origin_conversation_id,
            task_id=task.id,
        )
        await self._post(task.origin_conversation_id, note, report, ended)
        for t in tasks:
            self.publish(t)
        await self._push(task, f"After the call: {tally}", f"{len(tasks)} task(s) from the call.", raw=True)
        return True

    async def _post(self, origin: str, note: Message, report: Message, tasks: list[Task]) -> None:
        """The note and the report into the chat that asked, the files the tasks attached moved
        onto the report, and the chat marked unread when nobody is looking at it."""
        assert report.id is not None
        await self.core.store.add_message(note)
        await self.core.store.add_message(report)
        files = [
            a.id
            for t in tasks
            if t.conversation_id is not None
            for a in await self.core.attachments.for_conversation(t.conversation_id)
            if a.meta.get("outgoing")
        ]
        if files:
            report.attachments = await self.core.attachments.bind(files, message_id=report.id, conversation_id=origin)
        self.core.bus.publish(MessageCreated(message=note))
        self.core.bus.publish(MessageCreated(message=report))
        if not self.core.bus.has_subscribers(origin):
            conv = await self.core.store.update_conversation(origin, unread=1)
        else:
            conv = await self.core.store.get_conversation(origin)
        if conv is not None:
            self.core.bus.publish(ConversationUpdated(conversation=conv))

    # --- pushes -----------------------------------------------------------------------------------

    async def _push_waiting(self, task: Task) -> None:
        w = task.waiting
        what = w.name if w else "a step"
        if w and not task.incognito:
            args = ", ".join(f"{k}={_one_line(str(v), 40)}" for k, v in list(w.arguments.items())[:3])
            what = f"{w.name}({args})"
        await self._push(task, f"Task waits for your approval · {task.title}", what, raw=True)

    async def _push(self, task: Task, title: str, body: str, *, raw: bool = False) -> None:
        """One quiet notice that opens the card - never a one-tap approve (Gate B 2026-10-29),
        never Discord. An incognito task's notice carries no words from it."""
        text = body if raw else sentences(plain(body), 1)
        if task.incognito:
            text = "Open the chat to see it."
        path = f"/c/{task.origin_conversation_id}?task={task.id}"
        try:
            await self.core.notify.push(
                {
                    "type": "notice",
                    "tag": f"jarvis-task-{task.id}",
                    "path": path,
                    "title": title if not task.incognito else "Jarvis: a private task",
                    "body": clip(text, 160),
                    "link": self.core.notify.link(path),
                }
            )
        except Exception as exc:  # a push is a courtesy; the card and the report are the truth
            log.warning("task %s: push failed: %s", task.id, exc)

    # --- the sweeper ---------------------------------------------------------------------------------

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_s)
            try:
                await self.sweep()
            except Exception:
                log.exception("tasks sweep failed")

    async def sweep(self, now: datetime | None = None) -> None:
        """Calls whose page went quiet are ended (dropped). Approvals nobody answered in
        ``approval_ttl_s`` are turned down, with a note that makes the task read its source again
        before it asks anew."""
        at = now or _now()
        await self._sweep_calls(at)
        ttl = timedelta(seconds=self.settings.tasks.approval_ttl_s)
        for task in await self.store.list(states=(TaskState.WAITING,)):
            w = task.waiting
            if w is None or at - w.since < ttl:
                continue
            hours = ttl.total_seconds() / 3600
            note = (
                f"approval expired after {hours:g} h with no answer - re-read the source (the thread, the page) "
                "before you ask again, it may have changed"
            )
            task.note = "an approval expired unanswered"
            await self.store.save(task)
            await self.core.engine.confirm(w.run_id, w.call_id, False, note)

    # --- events out ------------------------------------------------------------------------------------

    def publish(self, task: Task) -> None:
        shown = task.model_copy(update={"paused": task.id in self._paused})
        self.core.bus.publish(TaskChanged(task=shown))

    async def removed(self, origin_conversation_id: str) -> list[str]:
        """The chat that asked is being deleted: its tasks' own conversations go with it."""
        out: list[str] = []
        for task in await self.store.list(origin=origin_conversation_id):
            if task.conversation_id:
                out.append(task.conversation_id)
            self.core.bus.publish(TaskRemoved(task_id=task.id))
        return out

    async def counts(self) -> dict[str, int]:
        rows = await self.core.db.fetchall("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
        return {r["state"]: r["n"] for r in rows if not TaskState(r["state"]).terminal}


# --- the tools ---------------------------------------------------------------------------------------


class _DodArg(BaseModel):
    item: str = Field(description="One checkable line: what will be true when it is done")
    outward: bool = Field(
        default=False,
        description="True when this item is done only by acting on someone else's behalf (a mail sent, a booking made)",
    )


class _BriefArgs(BaseModel):
    goal: str = Field(description="What the task delivers, in one or two sentences")
    context: str = Field(
        description=(
            "Everything from this conversation the task needs and cannot see: names, addresses, dates, amounts, "
            "links, file or attachment ids, what was already tried. The task sees nothing else of this chat."
        )
    )
    dod: list[_DodArg] = Field(min_length=1, max_length=8, description="The Definition of Done: 1-8 checkable items")
    constraints: list[str] = Field(default_factory=list, description="Limits it must keep (budget, dates, sources)")
    approvals: list[str] = Field(
        default_factory=list, description="What will need Arsen's OK (outward actions stop for it anyway)"
    )
    needs: list[str] = Field(
        default_factory=list,
        description="The tool namespaces it needs, e.g. browser, web, fetch, workocholic - checked against what is up",
    )
    assumptions: list[str] = Field(default_factory=list, description="What you assumed that he did not say")


class _DelegateArgs(_BriefArgs):
    title: str = Field(max_length=80, description="A few words for the card, in English")


class _NoteArgs(BaseModel):
    title: str = Field(max_length=80, description="A few words for the noted list, in English")
    needs: list[str] = Field(default_factory=list, description="Tool namespaces it will need, if you know them")


class _AmendArgs(BaseModel):
    task_id: str
    change: str = Field(description="What changes, as he said it")


class _TaskIdArgs(BaseModel):
    task_id: str


class _SteerArgs(BaseModel):
    task_id: str
    text: str = Field(description="What Arsen said about it - a change, an answer to its question - in his words")


class _ListArgs(BaseModel):
    everywhere: bool = Field(default=True, description="Open tasks from every chat, not only this one")


class _FinishDod(BaseModel):
    index: int = Field(ge=1, description="The DoD item's number in the brief")
    met: bool
    refs: list[str] = Field(
        default_factory=list, description="Ids of this task's tool calls (or attached files) that show it"
    )
    note: str = Field(default="", description="For an item not met: why, in a line")


class _FinishArgs(BaseModel):
    summary: str = Field(description="The result for Arsen in a few sentences, in English: what you found or did")
    dod: list[_FinishDod] = Field(description="Every DoD item, by its number")


class _AskArgs(BaseModel):
    question: str = Field(description="The one question only Arsen can answer, in English")


class _OriginArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=30, description="How many of the last messages")


class TasksTools(BuiltinProvider):
    """``tasks.*`` for the chat that hands work off, ``task.*`` for the task's own run. Which run
    sees which is TaskService's rule (``hidden``/``refusal``), checked again on every call."""

    name = "tasks"

    def __init__(self, core: Core, service: Callable[[], TaskService]) -> None:
        self._core = core
        self._service = service
        super().__init__()

    async def _run(self) -> Run | None:
        run_id = current_run_id.get()
        return await self._core.store.get_run(run_id) if run_id else None

    # --- the chat that asks ----------------------------------------------------------------------

    @tool(
        "tasks.delegate",
        description=(
            "Hand longer work to a background task and tell Arsen at once, so he does not wait: work that needs "
            "many tool steps or minutes (research across sites, a document, a comparison). A quick lookup or a "
            "short answer you do yourself, now. Before you hand anything off, be honest about it: if a tool it "
            "needs is down (see 'Not reachable now'), say you cannot do it now and why, and make no task; if a "
            "fact only he knows is missing (which document, which person, how much), ask him that one question "
            "instead; if what 'done' means is unclear, ask. The brief is all the task sees of this chat. After "
            "the call, tell him in one sentence that you started it and what you will deliver - do not do the "
            "work yourself. Two separate asks are two tasks."
        ),
        args=_DelegateArgs,
    )
    async def _delegate(
        self,
        title: str,
        goal: str,
        context: str,
        dod: list[dict[str, Any]],
        constraints: list[str],
        approvals: list[str],
        needs: list[str],
        assumptions: list[str],
        idempotency_key: str = "",
    ) -> ToolResult:
        run = await self._run()
        if run is None:
            return ToolResult.failure("a task can only be made from a chat's run")
        service = self._service()
        if (bad := self._unknown(needs)) is not None:
            return ToolResult.failure(bad)
        if (down := service.down_for(needs)) is not None:
            namespace, error, since = down
            return ToolResult.failure(
                f"{namespace} is not reachable (down since {service.clock(since)}: {error}). No task was made. "
                "Tell Arsen you cannot do this now and why, or ask whether he wants it once it is back."
            )
        brief = TaskBrief(
            goal=goal,
            context=context,
            constraints=constraints,
            dod=[DodItem.model_validate(d) for d in dod],
            approvals=approvals,
            needs=needs,
            assumptions=assumptions,
            asked=await service.asked_words(run.id),
        )
        task, made = await service.create(
            run=run, origin_key=_origin_key(idempotency_key, run), title=title, brief=brief, state=TaskState.QUEUED
        )
        if made:
            task = await service.begin(task)
        if task.state.terminal:
            reason = task.outcome.stopped_reason if task.outcome else ""
            return ToolResult.failure(f"the task could not start: {reason}. Tell Arsen.")
        ahead = service.queued_ahead(task, await service.store.list(open_only=True))
        queue = f"; {ahead} other task(s) go first" if ahead else ""
        return ToolResult.data(
            f'Task {task.id} "{task.title}" started{queue}. A card in this chat shows it, and the report comes '
            "here when it ends. Tell Arsen in one sentence that you took it and what you will deliver."
        )

    def _unknown(self, needs: list[str]) -> str | None:
        unknown = [n for n in needs if not self._core.registry.serves(n)]
        if not unknown:
            return None
        known = sorted({s.namespace for s in self._core.registry.specs()})
        return f"no tools in {', '.join(unknown)}; `needs` names tool namespaces: {', '.join(known)}"

    @tool(
        "tasks.list",
        description=(
            "The delegated tasks that are open - working, queued, waiting for Arsen's approval or his answer, "
            "noted on a call - with their state. Use it for 'what are you working on?'."
        ),
        args=_ListArgs,
        read_only=True,
        idempotent=True,
    )
    async def _list(self, everywhere: bool = True) -> ToolResult:
        here = current_conversation_id.get()
        service = self._service()
        tasks = await service.store.list(open_only=True, origin=None if everywhere else here)
        # Another chat's private task is not this chat's business.
        tasks = [t for t in tasks if not t.incognito or t.origin_conversation_id == here]
        if not tasks:
            return ToolResult.data("No open tasks.")
        lines = []
        for t in tasks:
            where = "" if t.origin_conversation_id == here else " (from another chat)"
            lines.append(f"{t.id} · {t.title} — {_detail(t)}{where}")
        return ToolResult.data("\n".join(lines))

    @tool(
        "tasks.cancel",
        description="Cancel a delegated task Arsen no longer wants. A working one stops at its next step.",
        args=_TaskIdArgs,
    )
    async def _cancel(self, task_id: str) -> ToolResult:
        task = await self._service().cancel(task_id)
        if task is None:
            return ToolResult.failure(f"no task {task_id}")
        return ToolResult.data(f'Cancelling "{task.title}"; the card says when it has stopped.')

    @tool(
        "tasks.steer",
        description=(
            "Pass on to a delegated task what Arsen said about it: a change of mind ('3 nights, not 2') or the "
            "answer to its question. A working task reads it at its next step; one that stopped carries on with it."
        ),
        args=_SteerArgs,
    )
    async def _steer(self, task_id: str, text: str) -> ToolResult:
        task = await self._service().steer(task_id, f"[from {self._core.settings.user_name}, via the chat] {text}")
        if task is None:
            return ToolResult.failure(f"no task {task_id}")
        return ToolResult.data(f'"{task.title}" has it ({_detail(task)}).')

    # --- on a call -------------------------------------------------------------------------------

    @tool(
        "tasks.note",
        description=(
            "On a call: note work that needs many steps or minutes, to be done after the call - a title is "
            "enough, his words are kept for you. Then say 'noted' in a few words and stay in the conversation. "
            "It starts by itself when the call ends; the report comes to this chat. Quick things you do now. "
            "If it is the same as something already noted, amend that one instead."
        ),
        args=_NoteArgs,
    )
    async def _note(self, title: str, needs: list[str], idempotency_key: str = "") -> ToolResult:
        run = await self._run()
        if run is None or not run.call_id:
            return ToolResult.failure("tasks.note works on a call")
        service = self._service()
        if (bad := self._unknown(needs)) is not None:
            return ToolResult.failure(bad)
        await service.call_seen(run.call_id, run.conversation_id)
        brief = TaskBrief(needs=needs, asked=await service.asked_words(run.id), written=False)
        task, _ = await service.create(
            run=run, origin_key=_origin_key(idempotency_key, run), title=title, brief=brief, state=TaskState.NOTED
        )
        noted = await service.store.list(call_id=run.call_id, states=(TaskState.NOTED,))
        return ToolResult.data(
            f'Noted: "{task.title}" ({task.id}; {len(noted)} noted on this call). Say "noted" in a few words. '
            "It starts when the call ends."
        )

    @tool(
        "tasks.drop",
        description="Take back something noted on this call ('forget the hotel one'). It will not start.",
        args=_TaskIdArgs,
    )
    async def _drop(self, task_id: str) -> ToolResult:
        service = self._service()
        task = await self._noted_here(task_id)
        if isinstance(task, ToolResult):
            return task
        await service.cancel(task.id)
        return ToolResult.data(f'Dropped "{task.title}".')

    @tool(
        "tasks.amend",
        description="Change something noted on this call ('3 nights, not 2'): his new words go with it.",
        args=_AmendArgs,
    )
    async def _amend(self, task_id: str, change: str) -> ToolResult:
        service = self._service()
        task = await self._noted_here(task_id)
        if isinstance(task, ToolResult):
            return task
        run = await self._run()
        words = await service.asked_words(run.id) if run else []
        task.brief.asked.extend(w for w in words if w not in task.brief.asked)
        task.brief.context = "\n".join(p for p in (task.brief.context, f"Changed on the call: {change.strip()}") if p)
        await service.store.save(task)
        service.publish(task)
        return ToolResult.data(f'"{task.title}" changed. It starts when the call ends.')

    async def _noted_here(self, task_id: str) -> Task | ToolResult:
        run = await self._run()
        task = await self._service().store.get(task_id)
        if task is None or run is None or task.call_id != run.call_id:
            return ToolResult.failure(f"nothing noted on this call as {task_id}; see the noted list")
        if task.state is not TaskState.NOTED:
            return ToolResult.failure(f'"{task.title}" is {_STATE_WORDS[task.state]}, not just noted')
        return task

    # --- the task's own run -------------------------------------------------------------------

    @tool(
        "task.finish",
        description=(
            "End the task: a short summary for Arsen in English and, for EVERY DoD item by its number, whether it "
            "is met and the ids of the tool calls (or attached files) in this task that show it. The code checks "
            "each id: an item without one that exists here and succeeded is shown as 'claimed, not verified'. "
            "After this, write your final reply."
        ),
        args=_FinishArgs,
        read_only=True,
    )
    async def _finish(self, summary: str, dod: list[dict[str, Any]]) -> ToolResult:
        service = self._service()
        run = await self._run()
        task = await service.store.by_run(run.id) if run else None
        if task is None:
            return ToolResult.failure("task.finish works only inside a delegated task")
        claimed: list[DodResult] = [DodResult(item=d.item) for d in task.brief.dod]
        for d in dod:
            i = int(d["index"]) - 1
            if 0 <= i < len(claimed):
                claimed[i] = DodResult(
                    item=claimed[i].item,
                    met=bool(d["met"]),
                    refs=[str(r) for r in d.get("refs") or []],
                    note=str(d.get("note") or "") or None,
                )
        checked = judge(task.brief, claimed, await ledger_of(self._core, task))
        task.outcome = (task.outcome or TaskOutcome()).model_copy(
            update={"summary": summary.strip(), "dod": checked, "finished": True}
        )
        await service.store.save(task)
        lines = [_dod_line(r) for r in checked]
        state = state_of(checked)
        return ToolResult.data(
            f"Recorded. As checked now the task is {_STATE_WORDS[state]}:\n"
            + "\n".join(lines)
            + "\nIf an id was wrong, call task.finish again with the right one; otherwise write your final reply."
        )

    @tool(
        "task.brief",
        description=(
            "Write this task's brief from his words and the call in the first message - before any other tool. "
            "The context must hold every name, date, amount and link it needs; the DoD 1-8 checkable items; "
            "what you assume that he did not say goes in assumptions. The card shows it to him."
        ),
        args=_BriefArgs,
        read_only=True,
    )
    async def _brief(
        self,
        goal: str,
        context: str,
        dod: list[dict[str, Any]],
        constraints: list[str],
        approvals: list[str],
        needs: list[str],
        assumptions: list[str],
    ) -> ToolResult:
        service = self._service()
        run = await self._run()
        task = await service.store.by_run(run.id) if run else None
        if task is None:
            return ToolResult.failure("task.brief works only inside a delegated task")
        if task.brief.written:
            return ToolResult.failure("the brief is already written; work from it")
        if (bad := self._unknown(needs)) is not None:
            return ToolResult.failure(bad)
        task.brief = task.brief.model_copy(
            update={
                "goal": goal,
                "context": context,
                "dod": [DodItem.model_validate(d) for d in dod],
                "constraints": constraints,
                "approvals": approvals,
                "needs": sorted({*task.brief.needs, *needs}),
                "assumptions": assumptions,
                "written": True,
            }
        )
        await service.store.save(task)
        service.publish(task)
        dod_lines = "\n".join(f"{i}. {d.item}" for i, d in enumerate(task.brief.dod, 1))
        return ToolResult.data(f"Brief written. Definition of Done:\n{dod_lines}\nNow do the work.")

    @tool(
        "task.ask",
        description=(
            "Ask Arsen the one thing only he knows (which document, which person, a budget) instead of guessing. "
            "The task waits for his answer; end your turn right after with the question as your reply."
        ),
        args=_AskArgs,
        read_only=True,
    )
    async def _ask(self, question: str) -> ToolResult:
        service = self._service()
        run = await self._run()
        task = await service.store.by_run(run.id) if run else None
        if task is None:
            return ToolResult.failure("task.ask works only inside a delegated task")
        task.outcome = (task.outcome or TaskOutcome()).model_copy(update={"question": question.strip()})
        await service.store.save(task)
        return ToolResult.data(
            "Asked. End your turn now with the question as your reply; his answer continues the task."
        )

    @tool(
        "task.origin",
        description="The last things said in the chat this task came from, to check a detail the brief left out.",
        args=_OriginArgs,
        read_only=True,
        idempotent=True,
    )
    async def _origin(self, limit: int = 10) -> ToolResult:
        service = self._service()
        run = await self._run()
        task = await service.store.by_run(run.id) if run else None
        if task is None:
            return ToolResult.failure("task.origin works only inside a delegated task")
        said = [
            m
            for m in await self._core.store.list_messages(task.origin_conversation_id, limit=200)
            if m.content.strip()
            and m.task_id is None
            and ((m.role is Role.USER and m.name is None) or (m.role is Role.ASSISTANT and not m.tool_calls))
        ][-limit:]
        user = self._core.settings.user_name
        lines = [
            f"[{m.created_at.strftime('%m-%d %H:%M')}] {user if m.role is Role.USER else 'Jarvis'}: "
            f"{m.content.strip()[:600]}"
            for m in said
        ]
        return ToolResult.data("\n".join(lines) or "Nothing was said there yet.")


def _origin_key(idempotency_key: str, run: Run) -> str:
    """One task per tool call: a resumed run re-running the same call gets the same key."""
    return idempotency_key.removesuffix(":retry") or f"{run.id}:{new_id('call')}"


def _waiting_line(task: Task) -> str:
    """What a task that waits on Arsen needs from him, for the after-call report."""
    if task.state is TaskState.WAITING and task.waiting:
        return f"It waits for your OK on {task.waiting.name} - approve or deny it on the task's card."
    if task.state is TaskState.NEEDS_INPUT and task.outcome and task.outcome.question:
        return f'It asks: "{_one_line(task.outcome.question, 300)}" - answer on the task\'s card.'
    return ""


def _detail(task: Task) -> str:
    words = _STATE_WORDS[task.state]
    if task.state is TaskState.WAITING and task.waiting:
        return f"{words}: {task.waiting.name}"
    if task.state is TaskState.NEEDS_INPUT and task.outcome and task.outcome.question:
        return f'{words}: "{_one_line(task.outcome.question, 160)}"'
    return words


# --- context ---------------------------------------------------------------------------------------


class TaskBlock:
    """The framing of a task's own run. In the stable prefix: the same text for every task, so it
    caches like the scheduled-run framing beside it."""

    stable = True

    def __init__(self, settings: Callable[[], Settings]) -> None:
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if run.kind is not RunKind.TASK:
            return None
        user = self._settings().user_name
        return (
            "## This task\n"
            f"You are working a task {user} handed you to do in the background while he gets on with other "
            "things. Nobody is watching live: your final reply and task.finish are the report he reads later, "
            "in the chat where he asked.\n"
            "- Work from the brief in the first message: its goal, its context and its Definition of Done. If "
            "the brief is not written yet (a task noted on a call), write it first with task.brief.\n"
            "- Every action that is not read-only - sending, posting, booking, typing into someone's site - "
            "stops for his approval by itself. Call it normally, with exactly the arguments you mean, and the "
            "task waits for his answer. If he turns it down, do not find another way around it.\n"
            "- Something only he knows (which document, which person, how much): task.ask with the one "
            "question, then end your turn. Never guess it.\n"
            "- End with task.finish: a short summary and every DoD item by number - met or not, with the ids "
            "of the tool calls or files that show it. An item you cannot show is not met.\n"
            "- A tool whose machine is down: say so; do not wait for it to come back.\n"
            f"- Write the summary and your reply to {user} in English; anything meant for other people stays "
            "in their language."
        )


class TasksBlock:
    """This chat's open tasks, and those that ended since its last turn - so "what are you working
    on?" needs no tool, the same ask twice is seen as such, and a report that landed during a call
    is mentioned at a pause. Per-turn context, and only when there is something to say."""

    def __init__(self, service: Callable[[], TaskService], settings: Callable[[], Settings]) -> None:
        self._service = service
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if run.kind is not RunKind.CHAT or not self._settings().tasks.enabled:
            return None
        service = self._service()
        tasks = await service.store.list(origin=run.conversation_id, limit=30)
        earlier = [r for r in await service.core.store.list_runs(run.conversation_id, limit=3) if r.id != run.id]
        since = earlier[0].created_at if earlier else None
        parts: list[str] = []
        if run.channel is Channel.VOICE:
            parts.append(_call_policy(run, self._settings()))
            noted = [t for t in tasks if run.call_id and t.call_id == run.call_id and t.state is TaskState.NOTED]
            if noted:
                lines = "\n".join(f'- {t.id} · {t.title}: "{_one_line(" / ".join(t.brief.asked), 300)}"' for t in noted)
                parts.append(f"Noted on this call so far (his words as heard):\n{lines}")
        live = [t for t in tasks if not t.state.terminal and t.state is not TaskState.NOTED]
        if live:
            parts.append("Open tasks in this chat:\n" + "\n".join(f"- {t.id} · {t.title} — {_detail(t)}" for t in live))
        new = [t for t in tasks if t.state.terminal and t.finished_at and since and t.finished_at > since]
        if new:
            lines = "\n".join(f"- {t.title} — {_STATE_WORDS[t.state]}" for t in new)
            parts.append(
                f"Ended since your last turn (the report is in the chat; mention it at a natural pause):\n{lines}"
            )
        if not parts:
            return None
        return "## Delegated tasks\n" + "\n\n".join(parts)


def _call_policy(run: Run, settings: Settings) -> str:
    """How a call treats work, when tasks are on. It replaces the old "do it now, never after the
    call" line of the voice style, and says so, because a style edited by hand may still have it."""
    user = settings.user_name
    how = (
        'note it with tasks.note (a title is enough; his words are kept for you) and say "noted" in a few '
        "words; it starts by itself when the call ends, and the report comes to this chat"
        if run.call_id
        else "hand it off with tasks.delegate and say in one sentence that you took it"
    )
    return (
        f"On this call: quick things - a lookup, an answer, one action - you do now. Work that needs many "
        f"steps or minutes, {how}. This takes precedence over any line in the call style that says to do "
        f"everything during the call. When {user} asks what you have, or is wrapping up, read the noted ones "
        "back in one sentence with the key details (who, when, how much) as he said them."
    )


class HealthBlock:
    """What cannot be reached right now, so "can I do this?" is answered from the real status and
    not by failing a call. Empty - no block, no tokens - when everything is up."""

    def __init__(self, registry: Callable[[], Any], settings: Callable[[], Settings]) -> None:
        self._registry = registry
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        s = self._settings()
        if run.kind is not RunKind.CHAT or not s.tasks.enabled:
            return None
        failing = self._registry().failing()
        if not failing:
            return None
        tz = ZoneInfo(s.timezone)
        lines = "\n".join(
            f"- {name}: down since {since.astimezone(tz).strftime('%H:%M')} ({_one_line(error, 160)})"
            for name, error, since in failing
        )
        return (
            "## Not reachable now\n"
            f"{lines}\n"
            "Their tools fail until they are back: do not promise work that needs them - say so, and why."
        )


def voice_style_upgrade(settings: Settings) -> tuple[Settings | None, str | None]:
    """Take the old "do it now, never after the call" sentence out of a stored call style, once.

    Settings are stored per top-level key, so a ``voice`` saved before tasks keeps the old default's
    text whatever the new default says - and Arsen's live style (edited 2026-10-20) has that exact
    sentence, verbatim, among his own lines. It is taken out and every other word of his is kept.
    Returns (settings to save, or None; a note for Settings → Voice when it could not be done).
    An exact sentence of our own old default, versioned by ``voice.style_version`` - not a word list.
    """
    voice = settings.voice
    if voice.style_version >= 2:
        return None, None
    style = voice.style
    note: str | None = None
    if CALL_WORK_NOW in style:
        style = style.replace(f"{CALL_WORK_NOW} ", "").replace(f" {CALL_WORK_NOW}", "").replace(CALL_WORK_NOW, "")
        log.info("voice style: the old 'never after the call' sentence was taken out; the rest is kept")
    elif style.strip() != VOICE_STYLE_DEFAULT.strip():
        note = (
            "This call style was edited before delegated tasks and does not have the old 'do it now, never after "
            "the call' sentence in its known form. If it tells Jarvis to do everything during the call, take that "
            "out: noted tasks need the call to end first. Saving the style here clears this note."
        )
        log.warning("voice style: the old 'never after the call' sentence was not found; left as it is")
        return None, note
    updated = settings.model_copy(deep=True)
    updated.voice.style = style
    updated.voice.style_version = 2
    return updated, note


def task_json(task: Task) -> dict[str, Any]:
    """A task as the REST API hands it out (the same shape as task.changed)."""
    return json.loads(task.model_dump_json())
