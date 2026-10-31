"""Run events (server → client, and the append-only ``run_events`` table) and client messages.

Every server frame is a ``ServerEvent``; every client frame is a ``ClientMessage``. Both are
discriminated on ``type``. Run-scoped events carry ``run_id``/``conversation_id`` and get a
``seq`` from the engine when persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from jarvis_proto.messages import Channel, Message, ToolResult
from jarvis_proto.runs import Conversation, ModelUsage, Plan, Run, RunKind, ThinkLevel
from jarvis_proto.tasks import Task


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


class RunSteered(RunEvent):
    """A message Arsen gave the run while it was working has been taken into the conversation.

    The message itself arrives as an ordinary ``message.created``; this says WHEN the run
    actually picked it up, so the transcript can show that it landed rather than leaving Arsen
    wondering whether it was heard.
    """

    type: Literal["run.steered"] = "run.steered"
    text: str


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


class ContextBreakdown(BaseModel):
    """Where a step's prompt goes, in estimated tokens (chars / 3.2), so the size of a prompt is
    something to read rather than to guess at: the stable system message, the tool schemas,
    earlier turns, this run's own tool results, and the window of the lane it is sent to."""

    system: int = 0
    tools: int = 0
    history: int = 0
    results: int = 0
    total: int = 0
    window: int | None = None
    # Tool results this step carries as an admitted head rather than whole.
    admitted: int = 0


class ModelCall(RunEvent):
    type: Literal["model.call"] = "model.call"
    role: str
    provider: str
    model: str
    message_count: int
    tool_count: int
    think: bool
    think_level: ThinkLevel | None = None
    context: ContextBreakdown | None = None


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


class FoldersChanged(_Base):
    """Arsen's chat folders were added to, renamed, reordered or removed.

    Conversation-level, so it reaches every client: a folder made on the phone has to appear
    in the tab on the desk without a reload.
    """

    type: Literal["folders.changed"] = "folders.changed"


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


class TaskChanged(_Base):
    """A delegated task moved (docs/stories/13_delegated_tasks.md).

    Conversation-level, so it reaches every client: the card lives in the chat that asked, the
    Tasks chip in the header of whichever chat is open, the noted list on the call screen.
    """

    type: Literal["task.changed"] = "task.changed"
    task: Task


class TaskRemoved(_Base):
    """A task is gone: its chat was deleted."""

    type: Literal["task.removed"] = "task.removed"
    task_id: str


ServerEvent = Annotated[
    RunQueued
    | RunStarted
    | RunResumed
    | RunSteered
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
    | FoldersChanged
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
    | MeetingChanged
    | TaskChanged
    | TaskRemoved,
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
    # Budget of another kind for this run (e.g. dry-running a scheduled prompt interactively
    # with the scheduled budget). None = the budget of `kind`.
    budget_kind: RunKind | None = None
    # Attachments uploaded before sending (POST /api/attachments), tied to this message.
    attachment_ids: list[str] = Field(default_factory=list)
    # Read only when conversation_id is None, i.e. when this message opens a new chat: an
    # incognito chat has to be one from its very first word (see Conversation.incognito), and
    # a disappearing one gets its idle time here rather than by a second request.
    incognito: bool = False
    ttl_seconds: int | None = None
    # "voice": the text came from the microphone on a call and the reply will be spoken — the
    # run answers briefly and plainly and is offered only the tools a call may use.
    channel: Channel = Channel.TEXT
    # Ring when this run ends (docs/stories/11_call_me.md): the bell's state when it was sent.
    # None = he did not touch it, and settings.call_me_default decides.
    call_me: bool | None = None
    # The sending device's IANA timezone (Intl.DateTimeFormat().resolvedOptions().timeZone).
    timezone: str | None = None
    # The call this voice turn belongs to: the web's id for one call, hang-up to hang-up. What a
    # task noted on the call hangs on (docs/stories/13_delegated_tasks.md).
    call_id: str | None = None


class RunCancelRequest(BaseModel):
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str


class RunCallMeRequest(BaseModel):
    """The bell, pressed while a run is ALREADY working: ring (or no longer ring) when it ends."""

    type: Literal["run.call_me"] = "run.call_me"
    run_id: str
    on: bool = True


class RunSteerRequest(BaseModel):
    """Something Arsen says to a run that is ALREADY working.

    Watching it head the wrong way and having to wait for it to finish is the worst moment in
    the loop. This hands it another message, which it reads at its next step exactly like the
    ones it started with — no restart, nothing thrown away.
    """

    type: Literal["run.steer"] = "run.steer"
    run_id: str
    text: str
    client_ref: str | None = None  # echoed back so the UI can match its optimistic bubble
    # A cut-in on a call is a steer too; when the run has already finished it becomes a voice
    # run of its own, and either way the words are marked as spoken in the transcript.
    channel: Channel = Channel.TEXT
    call_id: str | None = None


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


class Presence(BaseModel):
    """Whether Arsen can see this app right now (the page is visible, not a background tab or a
    locked phone). A ring by default waits for him to look away (features/callme.py)."""

    type: Literal["presence"] = "presence"
    visible: bool


class TaskCancelRequest(BaseModel):
    """Cancel from the card: a running task stops at its next step, a noted one never starts."""

    type: Literal["task.cancel"] = "task.cancel"
    task_id: str


class TaskSteerRequest(BaseModel):
    """Words for a task from its card: an answer to what it asked, or a change of mind. A
    working task reads them at its next step; one that has stopped carries on with them."""

    type: Literal["task.steer"] = "task.steer"
    task_id: str
    text: str


class CallAlive(BaseModel):
    """The call's page is still there - every 20 s from the web, and with every turn. Silence on
    the line is not a dropped call; a page that stopped saying this is (settings.tasks)."""

    type: Literal["call.alive"] = "call.alive"
    call_id: str
    conversation_id: str | None = None


class CallEnded(BaseModel):
    """Hang-up: what was noted on the call starts now."""

    type: Literal["call.ended"] = "call.ended"
    call_id: str


class TaskAllowToolRequest(BaseModel):
    """ "Always allow this tool in tasks" on a task's approval card: this call goes ahead and the
    tool joins settings.tasks.no_approval. ``call_id`` must be the call the task waits on."""

    type: Literal["task.allow_tool"] = "task.allow_tool"
    task_id: str
    call_id: str


ClientMessage = Annotated[
    RunCreateRequest
    | RunCancelRequest
    | RunCallMeRequest
    | RunSteerRequest
    | ToolConfirmRequest
    | Subscribe
    | Unsubscribe
    | Ping
    | Presence
    | TaskCancelRequest
    | TaskSteerRequest
    | CallAlive
    | CallEnded
    | TaskAllowToolRequest,
    Field(discriminator="type"),
]

client_message_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)


def parse_client_message(data: dict[str, Any]) -> Any:
    return client_message_adapter.validate_python(data)
