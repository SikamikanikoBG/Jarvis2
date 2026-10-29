"""Delegated tasks (docs/stories/13_delegated_tasks.md).

Until 2026-10-29 every request was a run Arsen watched until it ended; on a call he sat on the
line while Jarvis worked. A task is work handed off instead: a brief with a checkable Definition
of Done, done by a run of its own in a conversation of its own, and reported back - per DoD item,
with evidence the code has checked - in the chat that asked.

The task row is the one place that ties the three together: the chat that asked (``origin``), the
task's own conversation, and the run working in it. Its ``state`` is what the card shows; the
run's status stays the truth about the run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskState(StrEnum):
    #: Jotted down on a call; it starts when the call ends (hang-up or a dropped line).
    NOTED = "noted"
    QUEUED = "queued"
    RUNNING = "running"
    #: A step waits for Arsen's approval (every non-read-only action in a task does).
    WAITING = "waiting"
    #: The task asked him something only he knows; his answer continues it.
    NEEDS_INPUT = "needs_input"
    #: Every DoD item met, each with evidence the code found in the task's own log.
    DONE = "done"
    #: Some items met or claimed, not all verified.
    PARTIAL = "partial"
    #: Nothing met, or it stopped (budget, a tool that went down) before it could say.
    NOT_DONE = "not_done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: A noted task he took back on the call before it started.
    DROPPED = "dropped"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def settled(self) -> bool:
        """Nothing is going on in it now: it ended, or it waits for Arsen."""
        return self.terminal or self in (TaskState.WAITING, TaskState.NEEDS_INPUT)


_TERMINAL = frozenset(
    {
        TaskState.DONE,
        TaskState.PARTIAL,
        TaskState.NOT_DONE,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.DROPPED,
    }
)


class DodItem(BaseModel):
    """One checkable line of the Definition of Done, as the brief states it."""

    item: str
    # Done only by an action on someone else's behalf - a mail sent, a booking made. Such an
    # item counts as verified only against a call Arsen approved and that went out as itself
    # (not rewritten into a draft by the mail policy).
    outward: bool = False


class TaskBrief(BaseModel):
    """What the task is, written while the whole chat was still in front of the model."""

    goal: str = ""
    context: str = ""
    constraints: list[str] = Field(default_factory=list)
    dod: list[DodItem] = Field(default_factory=list)
    #: What will stop for his approval, in words (the code stops every outward action anyway).
    approvals: list[str] = Field(default_factory=list)
    #: The tool namespaces it needs (``browser``, ``workocholic``): checked against the real
    #: status of each when the task is created and again when it starts.
    needs: list[str] = Field(default_factory=list)
    #: What the brief took for granted that he did not say. Shown on the card.
    assumptions: list[str] = Field(default_factory=list)
    #: His own words the task came from, as stored, in the language he said them - copied by the
    #: code from the asking run's messages, never retyped by the model.
    asked: list[str] = Field(default_factory=list)
    #: False for a task noted on a call until its own first step writes the brief (task.brief).
    written: bool = True


class DodResult(BaseModel):
    """One DoD item after the task said how it went, and what the code made of its evidence."""

    item: str
    outward: bool = False
    #: What the task claimed: met, not met, or None when it never said.
    met: bool | None = None
    #: Tool call ids or attachment ids from this task's own work that show it.
    refs: list[str] = Field(default_factory=list)
    #: Every claim checked: met, and at least one ref that exists in this task and succeeded.
    verified: bool = False
    #: Why a claim was not verified, in a line ("c9 is not a call of this task").
    why: str | None = None
    note: str | None = None


class TaskOutcome(BaseModel):
    summary: str = ""
    dod: list[DodResult] = Field(default_factory=list)
    #: What it asked Arsen (task.ask); his answer continues it.
    question: str | None = None
    #: Why it ended without saying how it went: the budget, a host that went down, a restart.
    stopped_reason: str | None = None
    #: task.finish was called.
    finished: bool = False


class TaskWaiting(BaseModel):
    """The step that waits for his approval, exactly as the card shows it."""

    run_id: str
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    since: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Task(BaseModel):
    id: str
    title: str
    state: TaskState = TaskState.QUEUED
    brief: TaskBrief = Field(default_factory=TaskBrief)
    #: The chat that asked, and the run in it that called tasks.delegate / tasks.note.
    origin_conversation_id: str
    origin_run_id: str | None = None
    #: The call it was noted on (the web's call id); None for a typed delegation.
    call_id: str | None = None
    #: The task's own conversation (kind task) and the run working in it now. None while noted.
    conversation_id: str | None = None
    run_id: str | None = None
    depth: int = 0
    outcome: TaskOutcome | None = None
    waiting: TaskWaiting | None = None
    report_message_id: str | None = None
    #: Copied from the chat that asked: an incognito chat's task is as private as the chat.
    incognito: bool = False
    #: The last thing worth saying on the card that is not the outcome: "resumed after a restart",
    #: "an approval expired unanswered".
    note: str | None = None
    #: Held between steps while Arsen is on a call (the voice comes first on the GPU). Not stored.
    paused: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
