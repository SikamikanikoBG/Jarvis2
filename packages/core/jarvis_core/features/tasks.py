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
REPORT_ANSWER_CHARS = 6_000
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
            result.met, result.refs, result.note = said.met, [_ref_id(r) for r in said.refs], said.note
        if result.met is True:
            reasons = [why for ref in result.refs if (why := ledger.why_not(ref, outward=item.outward))]
            good = [ref for ref in result.refs if ledger.why_not(ref, outward=item.outward) is None]
            result.verified = bool(good)
            if not result.verified:
                result.why = "; ".join(reasons) if reasons else "no evidence given"
        out.append(result)
    return out


def _ref_id(ref: str) -> str:
    """A ref as the model may copy it from a result's header: "[ref c1]" or "ref c1" is c1."""
    bare = ref.strip().strip("[]").strip()
    return bare[4:].strip() if bare.lower().startswith("ref ") else bare


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


def render_report(task: Task, *, answer: str = "", attached: str | None = None) -> str:
    """The report in the chat that asked: the RESULT itself - the task's final answer, its table,
    its list, as written - then what the code verified about each DoD item, in English.

    Pass 2 of the alpha (2026-10-30, N-1): the report carried the task's own summary of its work
    ("I compiled a table of six rivers") and the table stayed in the hidden task conversation.
    The final answer is the deliverable; the summary stands in only when there is none. A result
    too long for a chat message is cut at a paragraph and attached whole (``attached``)."""
    outcome = task.outcome or TaskOutcome()
    head = f"**Task: {task.title} — {_STATE_WORDS[task.state]}**"
    parts = [head]
    result = answer.strip() or outcome.summary.strip()
    if attached:
        result += f"\n\n(The whole result is attached: {attached}.)"
    if result:
        parts.append(result)
    if outcome.dod:
        parts.append("\n".join(_dod_line(r) for r in outcome.dod))
    if outcome.stopped_reason:
        parts.append(f"Stopped: {outcome.stopped_reason}")
    return "\n\n".join(parts)


def split_result(text: str, limit: int = REPORT_ANSWER_CHARS) -> tuple[str, str | None]:
    """(what the report shows, the whole text to attach - or None when it all fits)."""
    body = text.strip()
    if len(body) <= limit:
        return body, None
    cut = body.rfind("\n\n", 0, limit)
    return body[: cut if cut > limit // 2 else limit].rstrip(), body


def brief_text(task: Task, *, user: str, call: str = "") -> str:
    """The task's first message: the brief as the task's model reads it. ``call``: the call it
    was noted on, as it went, for a task whose brief it writes itself."""
    b = task.brief
    lines = [f"# Task: {task.title}"]
    if not b.written:
        lines.append(
            "Its brief is not written yet: write it first, with task.brief - goal, context, a checkable "
            "Definition of Done - from his words and where he asked it, below."
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
            f"{i}. {d.item}" + (" (outward: done only by an action that went out as itself)" if d.outward else "")
            for i, d in enumerate(b.dod, 1)
        )
        lines.append("## Definition of Done\n" + dod)
    if b.approvals:
        lines.append("## Needs his approval\n" + "\n".join(f"- {a}" for a in b.approvals))
    if b.assumptions:
        lines.append("## Assumed (he did not say)\n" + "\n".join(f"- {a}" for a in b.assumptions))
    if call:
        lines.append(f"## Where he asked it (as it was said)\n{call}")
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
        #: The task runs holding a working slot (see should_hold).
        self._slots: set[str] = set()
        #: Words steered into a task's working run, until the run is seen to have read them.
        self._steered: dict[str, list[str]] = {}
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
        # Switched off, no new task is made - but a task already working finishes properly: its
        # own task.* tools stay (alpha F-13: flag off mid-flight turned a finished task "not done").
        if namespace == "tasks" and not s.enabled:
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

    async def unreachable(self, run: Run, namespace: str, reason: str) -> bool:
        """The task ends on a machine that is down only when its brief needs that machine."""
        task = await self.store.by_run(run.id)
        if task is None or namespace not in task.brief.needs:
            return False
        task.outcome = (task.outcome or TaskOutcome()).model_copy(update={"stopped_reason": reason})
        await self.store.save(task)
        return True

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

    def should_hold(self, run: Run) -> bool:
        """A task waits before its next step: Arsen is on a call, or the working slots
        (``tasks.max_running``) are taken. A task parked on an approval gives its slot up and takes
        one again before its next step - so one let go by an approval queues behind the task that
        started meanwhile, instead of running beside it (alpha F-7)."""
        if self.call_open():
            return True
        self._slots &= set(self.core.engine.active_run_ids())
        if run.id in self._slots:
            return False
        if len(self._slots) < self.settings.tasks.max_running:
            self._slots.add(run.id)
            return False
        return True

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
            source = ""
            if not task.brief.written:
                # The task writes its own brief, from the call it was noted on or the chat it was
                # handed off in: the asking turn only names it, and the chat is free in seconds (F-8).
                source = await self.call_transcript(task) if task.call_id else await self.origin_transcript(task)
            text = brief_text(task, user=user, call=source)
        run, _ = await self.core.engine.create_run(
            text=text,
            conversation_id=task.conversation_id,
            kind=RunKind.TASK,
            call_me=False,
        )
        task.run_id = run.id
        task.state = TaskState.QUEUED
        task.finished_at = None
        task.report_message_id = None
        task.note = None
        task.waiting = None
        if task.outcome is not None:
            # A new round (an answer, a change of mind): what was claimed and shown before stays
            # claimed; this round's result and its own task.finish are what the next report is
            # about (N-1: a steer to a finished task reported the old result again).
            task.outcome = task.outcome.model_copy(
                update={"question": None, "stopped_reason": None, "finished": False, "summary": ""}
            )
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

    async def origin_transcript(self, task: Task, *, limit: int = 20) -> str:
        """The chat a task was handed off in, up to the turn that handed it off: what its first
        step writes the brief from - copied by the code, never retold by a model."""
        user = self.settings.user_name
        said: list[str] = []
        upto: int | None = None  # what was said up to the handing-off turn's own words
        for m in await self.core.store.list_messages(task.origin_conversation_id, limit=400):
            if m.task_id is not None or not m.content.strip() or m.name is not None:
                continue
            if m.role is Role.USER:
                said.append(f"{user}: {m.content.strip()}")
                if m.run_id == task.origin_run_id:
                    upto = len(said)
            elif m.role is Role.ASSISTANT and not m.tool_calls and m.run_id != task.origin_run_id:
                said.append(f"Jarvis: {m.content.strip()[:1500]}")
        return "\n".join(said[: upto if upto is not None else len(said)][-limit:])

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

    async def allow_tool(self, task_id: str, call_id: str) -> str | None:
        """ "Always allow this tool in tasks": the call the task waits on goes ahead, and its tool joins
        ``settings.tasks.no_approval`` - visible and removable in Settings -> Tasks. None when done,
        otherwise why not. Only the tool of the approval he is looking at, and never one of
        ``deny_tools`` (shell, file writes stay out of tasks whatever he clicks)."""
        task = await self.store.get(task_id)
        if task is None:
            return "no such task"
        w = task.waiting
        if task.state is not TaskState.WAITING or w is None or w.call_id != call_id:
            return "the task is not waiting on that call"
        s = self.settings
        if s.tasks.denies(w.name):
            return f"{w.name} is never offered to a task (settings.tasks.deny_tools)"
        if not s.tasks.frees(w.name):
            updated = s.model_copy(deep=True)
            updated.tasks.no_approval = [*s.tasks.no_approval, w.name]
            await self.core.store.save_settings(updated, only_keys={"tasks"})
            self.core.apply_settings(updated)
            log.info("task %s: %s is now allowed in tasks without asking", task.id, w.name)
        await self.core.engine.confirm(w.run_id, w.call_id, True, "always allowed in tasks from now on")
        return None

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
            # Taken by a working run - unless it ends before its next step: then the words carry
            # on into a new round when it does, instead of being lost (F-12).
            self._steered.setdefault(task.id, []).append(words)
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
                    task.started_at = task.started_at or _now()  # queued is not started (L-4)
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
                self._slots.discard(event.run_id)  # parked on him: the slot is free for another task
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
        unread = await self._unread_steers(task, run)
        if run.status is RunStatus.CANCELLED:
            outcome.stopped_reason = outcome.stopped_reason or "cancelled by Arsen"
            # What it had claimed stays on the card: a cancel does not undo what was done.
            outcome.dod = judge(task.brief, outcome.dod if outcome.finished else [], await ledger_of(self.core, task))
            task.outcome = outcome
            await self._end(task, TaskState.CANCELLED)
            return
        if unread and run.status is RunStatus.DONE and not outcome.finished:
            # He said something it never got to read: it carries on with that, not a report yet.
            await self._start(task, "\n\n".join(unread))
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
        if outcome.finished or any(d.met is not None for d in outcome.dod):
            # Finished now, or a later round of a task that had already said how it went.
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

    async def _unread_steers(self, task: Task, run: Run) -> list[str]:
        pending = self._steered.pop(task.id, [])
        if not pending:
            return []
        read = {m.content.strip() for m in await self.core.store.list_run_messages(run.id) if m.role is Role.USER}
        return [w for w in pending if w not in read]

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
        # A named answer is the core's own line ("I stopped here: ..."), said by the report already.
        said = [m for m in msgs if m.role is Role.ASSISTANT and m.content.strip() and m.name is None]
        final = next((m for m in reversed(said) if not m.tool_calls), None)
        # A cancelled or failed round has no final answer: its report shows the work it got to (F-16).
        return final or (said[-1] if said and task.state in (TaskState.CANCELLED, TaskState.FAILED) else None)

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
        shown, whole = split_result(answer.content if answer else "")
        name = f"{_file_stem(task.title)}.md" if whole else None
        report = Message.assistant(
            render_report(task, answer=shown, attached=name),
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
        extra = await self._attach_whole(task, whole, name)
        await self._post(task.origin_conversation_id, note, report, [task], extra=extra)
        self.publish(task)
        outcome = task.outcome or TaskOutcome()
        body = (answer.content if answer else "") or outcome.summary or outcome.stopped_reason or ""
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
        extra: list[str] = []
        for t in tasks:
            if t.state.terminal:
                answer = await self._final_answer(t)
                shown, whole = split_result(answer.content if answer else "")
                name = f"{_file_stem(t.title)}.md" if whole else None
                extra += await self._attach_whole(t, whole, name)
                sections.append(render_report(t, answer=shown, attached=name))
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
        await self._post(task.origin_conversation_id, note, report, ended, extra=extra)
        for t in tasks:
            self.publish(t)
        await self._push(task, f"After the call: {tally}", f"{len(tasks)} task(s) from the call.", raw=True)
        return True

    async def _attach_whole(self, task: Task, whole: str | None, name: str | None) -> list[str]:
        """A result too long for the report, as a file of its own (in the task's privacy)."""
        if not whole or not name:
            return []
        att = await self.core.attachments.add_text(
            text=whole,
            name=name,
            conversation_id=task.origin_conversation_id,
            meta={"outgoing": True},
            incognito=task.incognito,
        )
        return [att.id]

    async def _post(
        self, origin: str, note: Message, report: Message, tasks: list[Task], *, extra: list[str] | None = None
    ) -> None:
        """The note and the report into the chat that asked, the files the tasks attached (and a
        long result's whole text) moved onto the report, and the chat marked unread when nobody
        is looking at it."""
        assert report.id is not None
        await self.core.store.add_message(note)
        await self.core.store.add_message(report)
        files = [
            a.id
            for t in tasks
            if t.conversation_id is not None
            for a in await self.core.attachments.for_conversation(t.conversation_id)
            if a.meta.get("outgoing")
        ] + list(extra or [])
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


class _DelegateArgs(BaseModel):
    # A title is all the asking turn has to write: the task writes its own brief, from his words
    # and this chat, as its first step. The alpha's acks took 30-150 s writing briefs (F-8).
    title: str = Field(description="A few words for the card, in English (a long one is shortened)")
    context: str = Field(
        default="",
        description=(
            "Only what the task cannot find in his words or in this chat's recent messages: something from "
            "much earlier, an id, a file. Usually empty."
        ),
    )
    needs: list[str] = Field(
        default_factory=list,
        description="The tool namespaces it needs, e.g. browser, web, fetch, workocholic - checked against what is up",
    )
    goal: str = Field(default="", description="Optional: what it delivers. Without it the task writes its own brief.")
    dod: list[_DodArg] = Field(
        default_factory=list,
        max_length=8,
        description="Optional: the Definition of Done. Without it the task writes it.",
    )
    constraints: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class _NoteArgs(BaseModel):
    title: str = Field(description="A few words for the noted list, in English (a long one is shortened)")
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
        default_factory=list,
        description="The [ref ...] ids at the top of this task's tool results that show it, e.g. ['c3', 'c7']",
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
            "short answer you do yourself, now. A short title is enough: his words and this chat go with it, "
            "and the task writes its own brief and Definition of Done. Before you hand anything off, be honest: "
            "if what it needs is not connected or is down (see 'What you can reach now'), say you cannot do it "
            "and why, and make no task; if a fact only he knows is missing (which document, which person, how "
            "much), ask him that one question instead. Two separate asks are two tasks - hand both off in the "
            "same reply. After it, your turn is the one-line answer: you took it, what will come back."
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
        asked = await service.asked_words(run.id)
        # The same words in the same chat while that task is still open: the same ask, not a new
        # task (a double send, or the ask repeated) - F-9. Compared as stored, not interpreted.
        for open_task in await service.store.list(origin=run.conversation_id, open_only=True):
            if asked and open_task.brief.asked == asked and open_task.origin_run_id != run.id:
                result = ToolResult.data(
                    f'Already on it: "{open_task.title}" is {_STATE_WORDS[open_task.state]} - no second task. '
                    "Tell Arsen so in one line."
                )
                return result.model_copy(update={"ends_turn": True})
        items = [DodItem.model_validate(d) for d in dod]
        brief = TaskBrief(
            goal=goal,
            context=context,
            constraints=constraints,
            dod=items,
            approvals=approvals,
            needs=needs,
            assumptions=assumptions,
            asked=asked,
            written=bool(items),  # no DoD given: the task writes its brief as its first step
        )
        task, made = await service.create(
            run=run,
            origin_key=_origin_key(idempotency_key, run),
            title=_short_title(title),
            brief=brief,
            state=TaskState.QUEUED,
        )
        if made:
            task = await service.begin(task)
        if task.state.terminal:
            reason = task.outcome.stopped_reason if task.outcome else ""
            return ToolResult.failure(f"the task could not start: {reason}. Tell Arsen.")
        return ToolResult.data(_ack(task, await service.store.list(open_only=True), service)).model_copy(
            update={"ends_turn": True}
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
        # Checked when it is noted too, not only when it starts: on a call he is still listening,
        # so he hears "can't, X is down" now instead of a promise that ends "not started".
        if (down := service.down_for(needs)) is not None:
            namespace, error, since = down
            return ToolResult.failure(
                f"{namespace} is not reachable (down since {service.clock(since)}: {error}). Nothing was noted. "
                "Tell him now that you cannot do this and why, or do it another way with what is connected."
            )
        await service.call_seen(run.call_id, run.conversation_id)
        brief = TaskBrief(needs=needs, asked=await service.asked_words(run.id), written=False)
        task, _ = await service.create(
            run=run,
            origin_key=_origin_key(idempotency_key, run),
            title=_short_title(title),
            brief=brief,
            state=TaskState.NOTED,
        )
        noted = await service.store.list(call_id=run.call_id, states=(TaskState.NOTED,))
        result = ToolResult.data(
            f'Noted: "{task.title}" ({len(noted)} noted on this call). Say "noted" in a few words and stay in the '
            "conversation; it starts when the call ends."
        )
        return result.model_copy(update={"ends_turn": True})

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
            "End the task: the result for Arsen in English (the substance - what you found or did, not a status "
            "line) and, for EVERY DoD item by its number, whether it is met and its refs: the id at the top of "
            "each tool result that shows it, as in [ref c3] (or the ref files.attach gave for a file). The code "
            "checks each ref: one that is not a successful result of this task leaves the item 'claimed, not "
            "verified'. After this, write your final reply."
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


def _file_stem(title: str) -> str:
    """A file name from a title: its words, nothing a file system minds."""
    words = "".join(ch if ch.isalnum() else " " for ch in title).split()
    return "-".join(words)[:60] or "result"


#: How long a card's title may be; a longer one is shortened, never refused (L-5).
TITLE_CHARS = 80


def _short_title(title: str) -> str:
    return clip(" ".join(title.split()), TITLE_CHARS) or "Task"


def _ack(task: Task, open_tasks: list[Task], service: TaskService) -> str:
    """What the asking turn is told it may say: started or queued, as it is - never "done" (F-14)."""
    ahead = service.queued_ahead(task, open_tasks)
    if service.call_open():
        when = "It is queued: it starts once the call is over."
    elif ahead >= service.settings.tasks.max_running:
        when = f"It is queued behind {ahead} other task(s) and starts when they are through."
    else:
        when = "It starts now."
    return (
        f'Handed off: "{task.title}". {when} The card in this chat shows it; the report comes here when it ends. '
        "Now answer Arsen in one short line - that you took it and what will come back. It is not done yet."
    )


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
            "the brief is not written yet, write it first with task.brief: the goal and the Definition of Done "
            f"come from what {user} asked for. What Jarvis said back to him there was from memory and is not "
            "checked - never build it into the brief as a fact; check it with a tool if it matters.\n"
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
    # The default on the line is to note: the alpha's call searched and fetched for minutes while
    # he held on, because "quick things now" left every judgement call on the side of doing (F-3).
    return (
        f"On this call, answer from what you know, and do at most one quick lookup. Anything that needs "
        f"more - searching, reading pages, comparing, writing something, booking, sending - {how}, unless "
        f"{user} says he wants it done now, during the call. This takes precedence over any line in the "
        f"call style that says to do everything during the call. When he asks what you have, or is wrapping "
        "up, read the noted ones back in one sentence with the key details (who, when, how much) as he said them."
    )


class HealthBlock:
    """What Jarvis can reach right now, outside the core, so "can I do this?" is answered from the
    real status and not by failing a call: what is connected, what is down and since when, and that
    nothing else is. The alpha (2026-10-30, F-10) had no laptop host at all and Jarvis still offered
    to read a file from the laptop - a block that listed only what was DOWN said nothing about a
    machine that was never there. Changes rarely, so the context dedup sends it once."""

    def __init__(self, registry: Callable[[], Any], settings: Callable[[], Settings]) -> None:
        self._registry = registry
        self._settings = settings

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        s = self._settings()
        if run.kind is not RunKind.CHAT or not s.tasks.enabled:
            return None
        links = self._registry().connections()
        if not links:
            return None
        tz = ZoneInfo(s.timezone)
        up = [name for name, error, _since in links if error is None]
        down = [
            f"- {name}: not available"
            + (f" since {since.astimezone(tz).strftime('%H:%M')}" if since else "")
            + f" ({_one_line(error, 160)})"
            for name, error, since in links
            if error is not None
        ]
        lines = [f"Connected: {', '.join(up) if up else 'nothing'}."]
        if down:
            lines.append("Not available:\n" + "\n".join(down))
        lines.append(
            "No other machine, host or service is connected. Work that needs one that is not connected here "
            "cannot be done now: say so, and why, before you start - and do not hand it to a task."
        )
        return "## What you can reach now\n" + "\n".join(lines)


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
