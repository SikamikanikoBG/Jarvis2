"""Run events (server → client, and the append-only ``run_events`` table) and client messages.

Every server frame is a ``ServerEvent``; every client frame is a ``ClientMessage``. Both are
discriminated on ``type``. Run-scoped events carry ``run_id``/``conversation_id`` and get a
``seq`` from the engine when persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from jarvis_proto.messages import Message, ToolResult
from jarvis_proto.runs import Conversation, ModelUsage, Plan, Run, RunKind, ThinkLevel


class _Base(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunEvent(_Base):
    """Base for everything that belongs to a run."""

    run_id: str
    conversation_id: str
    seq: int = 0


# --- run lifecycle -----------------------------------------------------------------------


class RunQueued(RunEvent):
    type: Literal["run.queued"] = "run.queued"
    kind: RunKind
    input_preview: str = ""
    user_message_id: str | None = None


class RunStarted(RunEvent):
    type: Literal["run.started"] = "run.started"


class RunResumed(RunEvent):
    type: Literal["run.resumed"] = "run.resumed"
    from_seq: int


class RunWaitingUser(RunEvent):
    type: Literal["run.waiting_user"] = "run.waiting_user"
    reason: str
    call_id: str | None = None


class RunDone(RunEvent):
    type: Literal["run.done"] = "run.done"
    message_id: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    steps_used: int = 0
    summary: str | None = None  # set when a budget ended the run


class RunFailed(RunEvent):
    type: Literal["run.failed"] = "run.failed"
    error: str


class RunCancelled(RunEvent):
    type: Literal["run.cancelled"] = "run.cancelled"
    partial_message_id: str | None = None


class RunInterrupted(RunEvent):
    type: Literal["run.interrupted"] = "run.interrupted"


# --- planning ---------------------------------------------------------------------------


class PlanCreated(RunEvent):
    type: Literal["plan.created"] = "plan.created"
    plan: Plan


class PlanStepStarted(RunEvent):
    type: Literal["plan.step_started"] = "plan.step_started"
    index: int
    title: str


class PlanStepDone(RunEvent):
    type: Literal["plan.step_done"] = "plan.step_done"
    index: int


# --- model ------------------------------------------------------------------------------


class ModelCall(RunEvent):
    type: Literal["model.call"] = "model.call"
    role: str
    provider: str
    model: str
    message_count: int
    tool_count: int
    think: bool
    think_level: ThinkLevel | None = None


class ModelDelta(RunEvent):
    type: Literal["model.delta"] = "model.delta"
    kind: Literal["text", "reasoning"]
    text: str


class ModelDone(RunEvent):
    type: Literal["model.done"] = "model.done"
    usage: ModelUsage
    finish_reason: str | None = None
    tool_call_count: int = 0


# --- tools ------------------------------------------------------------------------------


class ToolCallEvent(RunEvent):
    type: Literal["tool.call"] = "tool.call"
    call_id: str
    name: str
    arguments: dict[str, Any]
    read_only: bool
    idempotency_key: str


class ToolResultEvent(RunEvent):
    type: Literal["tool.result"] = "tool.result"
    call_id: str
    name: str
    result: ToolResult
    duration_ms: int


class ToolConfirmRequested(RunEvent):
    type: Literal["tool.confirm_requested"] = "tool.confirm_requested"
    call_id: str
    name: str
    arguments: dict[str, Any]
    reason: str


class ToolConfirmResolved(RunEvent):
    type: Literal["tool.confirm_resolved"] = "tool.confirm_resolved"
    call_id: str
    approved: bool
    note: str | None = None


# --- supervision ------------------------------------------------------------------------


class GuardArmed(RunEvent):
    type: Literal["guard.armed"] = "guard.armed"
    guard: str
    detail: str


class GuardConsumed(RunEvent):
    type: Literal["guard.consumed"] = "guard.consumed"
    guard: str
    detail: str


class JudgeVerdict(RunEvent):
    type: Literal["judge.verdict"] = "judge.verdict"
    verdict: Literal["continue", "nudge", "stop"]
    reason: str


# --- conversation-level (not run-scoped) ------------------------------------------------


class ConversationUpdated(_Base):
    type: Literal["conversation.updated"] = "conversation.updated"
    conversation: Conversation


class ConversationDeleted(_Base):
    type: Literal["conversation.deleted"] = "conversation.deleted"
    conversation_id: str


class MessageCreated(_Base):
    type: Literal["message.created"] = "message.created"
    message: Message


class RunUpdated(_Base):
    type: Literal["run.updated"] = "run.updated"
    run: Run


class Pong(_Base):
    type: Literal["pong"] = "pong"


# --- features (Phases 2–7) --------------------------------------------------------------


class ContextSkills(RunEvent):
    type: Literal["context.skills"] = "context.skills"
    names: list[str]


class BoardChanged(_Base):
    type: Literal["board.changed"] = "board.changed"
    board_id: str | None = None


class KgChanged(_Base):
    type: Literal["kg.changed"] = "kg.changed"
    entity_ids: list[str] = Field(default_factory=list)


class SkillsChanged(_Base):
    type: Literal["skills.changed"] = "skills.changed"


class ScheduleChanged(_Base):
    type: Literal["schedule.changed"] = "schedule.changed"
    schedule_id: str | None = None


class ToolsChanged(_Base):
    type: Literal["tools.changed"] = "tools.changed"
    provider: str | None = None


class MeetingSegment(_Base):
    type: Literal["meeting.segment"] = "meeting.segment"
    meeting_id: str
    conversation_id: str
    seq: int
    t0: float
    t1: float
    text: str


class MeetingChanged(_Base):
    type: Literal["meeting.changed"] = "meeting.changed"
    meeting_id: str
    conversation_id: str
    status: str


ServerEvent = Annotated[
    RunQueued
    | RunStarted
    | RunResumed
    | RunWaitingUser
    | RunDone
    | RunFailed
    | RunCancelled
    | RunInterrupted
    | PlanCreated
    | PlanStepStarted
    | PlanStepDone
    | ModelCall
    | ModelDelta
    | ModelDone
    | ToolCallEvent
    | ToolResultEvent
    | ToolConfirmRequested
    | ToolConfirmResolved
    | GuardArmed
    | GuardConsumed
    | JudgeVerdict
    | ConversationUpdated
    | ConversationDeleted
    | MessageCreated
    | RunUpdated
    | Pong
    | ContextSkills
    | BoardChanged
    | KgChanged
    | SkillsChanged
    | ScheduleChanged
    | ToolsChanged
    | MeetingSegment
    | MeetingChanged,
    Field(discriminator="type"),
]

server_event_adapter: TypeAdapter[Any] = TypeAdapter(ServerEvent)


def parse_server_event(data: dict[str, Any]) -> Any:
    return server_event_adapter.validate_python(data)


# --- client → server --------------------------------------------------------------------


class RunCreateRequest(BaseModel):
    type: Literal["run.create"] = "run.create"
    conversation_id: str | None = None  # None → create a new chat conversation
    text: str
    kind: RunKind = RunKind.CHAT
    client_ref: str | None = None  # echoed in run.queued so the UI can correlate
    # Per-message thinking override; None = the chat role's configured setting.
    think: bool | None = None
    think_level: ThinkLevel | None = None


class RunCancelRequest(BaseModel):
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str


class ToolConfirmRequest(BaseModel):
    type: Literal["tool.confirm"] = "tool.confirm"
    run_id: str
    call_id: str
    approved: bool
    note: str | None = None


class Subscribe(BaseModel):
    type: Literal["subscribe"] = "subscribe"
    conversation_id: str


class Unsubscribe(BaseModel):
    type: Literal["unsubscribe"] = "unsubscribe"
    conversation_id: str


class Ping(BaseModel):
    type: Literal["ping"] = "ping"


ClientMessage = Annotated[
    RunCreateRequest | RunCancelRequest | ToolConfirmRequest | Subscribe | Unsubscribe | Ping,
    Field(discriminator="type"),
]

client_message_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)


def parse_client_message(data: dict[str, Any]) -> Any:
    return client_message_adapter.validate_python(data)
