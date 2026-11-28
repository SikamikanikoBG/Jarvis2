"""Feature models shared by REST, tools and the web client (Phases 2–7)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis_proto.runs import ThinkLevel

# --- boards -----------------------------------------------------------------------------

NoteColor = Literal["yellow", "blue", "green", "pink", "grey"]


class Board(BaseModel):
    id: str
    name: str
    position: int = 0
    note_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Note(BaseModel):
    id: str
    board_id: str
    text: str
    color: NoteColor = "yellow"
    from_message_id: str | None = None
    position: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- knowledge --------------------------------------------------------------------------

EntityType = Literal["person", "org", "project", "place", "thing", "topic"]


class Entity(BaseModel):
    id: str
    name: str
    type: EntityType = "thing"
    summary: str = ""
    aliases: list[str] = Field(default_factory=list)
    mention_count: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Edge(BaseModel):
    src: str
    dst: str
    relation: str
    weight: float = 1.0
    evidence: str | None = None


class Mention(BaseModel):
    conversation_id: str | None
    message_id: str | None
    snippet: str | None
    at: datetime


class EdgeWithOther(Edge):
    other: Entity


class EntityDetail(Entity):
    edges: list[EdgeWithOther] = Field(default_factory=list)
    mentions: list[Mention] = Field(default_factory=list)


class Graph(BaseModel):
    nodes: list[Entity]
    edges: list[Edge]


# --- skills -----------------------------------------------------------------------------


class Skill(BaseModel):
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    enabled: bool = True
    size: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- schedules --------------------------------------------------------------------------

CatchUp = Literal["skip", "run_once"]
# Whether a scheduled task rings Arsen when it ends (docs/stories/11_call_me.md): "default" leaves
# it to settings.call_me_default; "never" holds even when the run itself asks (jarvis.call_me).
ScheduleCallMe = Literal["default", "always", "never"]


class Schedule(BaseModel):
    id: str
    name: str
    prompt: str
    cron: str | None = None
    at: datetime | None = None
    tz: str = "Europe/Sofia"
    enabled: bool = True
    catch_up: CatchUp = "skip"
    think: bool | None = None
    think_level: ThinkLevel | None = None
    call_me: ScheduleCallMe = "default"
    # Who wrote the prompt: None = Arsen (in the UI, or Jarvis at his asking in his own chat); a
    # run id = a run nobody was asking from. Such a prompt is standing authority nobody approved,
    # so it fires with a task's approvals until he accepts it (docs/stories/14_agent_admin.md).
    prompt_by: str | None = None
    # The project this schedule serves (features/projects.py); None = on its own.
    project_id: str | None = None
    next_fire: datetime | None = None
    last_fired_for: datetime | None = None
    last_run_id: str | None = None
    last_status: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Project(BaseModel):
    """Several schedules that serve one bigger goal. When one of them fires, its run is told the
    goal, the brief and what the others last reported (features/projects.py)."""

    id: str
    name: str
    goal: str = ""
    brief: str = ""
    ends_at: datetime | None = None
    position: int = 0
    schedule_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


LocationSource = Literal["browser", "owntracks", "manual"]


class DeviceLocation(BaseModel):
    """One position fix from one of his devices (core features/location.py)."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    #: Radius of the fix in metres, as the device reports it.
    accuracy_m: float | None = Field(default=None, ge=0)
    #: When the device took the fix (not when it arrived).
    taken_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: LocationSource = "browser"
    #: Speed in m/s when the device knows it - walking, driving, flying.
    speed_mps: float | None = Field(default=None, ge=0)


class Place(BaseModel):
    """A place he named - Home, Office, Gym - so Jarvis says "at home", not an address."""

    id: str
    name: str
    lat: float
    lon: float
    radius_m: float = 150
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LocationView(BaseModel):
    """What the core makes of the last fix: the fix, the town it is in, and the named place."""

    fix: DeviceLocation | None = None
    received_at: datetime | None = None
    locality: str | None = None
    country: str | None = None
    place: str | None = None
    #: Distance from Home in km, when a place named Home exists.
    from_home_km: float | None = None
    #: still | walking | running | cycling | vehicle | flying | unknown, from speed
    #: (features/location.py classify). None before the second fix.
    motion: str | None = None
    #: Since when he has been in that state (as of the fix).
    motion_since: datetime | None = None
    motion_kmh: float | None = None


NavMode = Literal["walking", "cycling", "driving"]


class NavStep(BaseModel):
    """One manoeuvre of a route, worded for speaking (core features/navigation.py)."""

    instruction: str
    lat: float
    lon: float
    #: Length of the stretch after this manoeuvre, to the next one, in metres.
    distance_m: float = 0
    kind: str = "turn"
    landmark: str | None = None


class NavView(BaseModel):
    """What the phone shows while Jarvis guides him: the next manoeuvre and what is left."""

    destination: str
    mode: NavMode
    next_instruction: str | None = None
    next_landmark: str | None = None
    to_next_m: float | None = None
    left_m: float | None = None
    eta_s: float | None = None
    step: int = 0
    steps: int = 0
    conversation_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScheduleFire(BaseModel):
    schedule_id: str
    scheduled_for: datetime
    run_id: str | None = None
    conversation_id: str | None = None
    status: str | None = None


# --- agent admin: the trail of changes (docs/stories/14_agent_admin.md) ------------------

ChangeArea = Literal["settings", "schedule", "api"]
# applied: in force. probing: a lifeline change in force, still being re-checked. proposed: a risky
# change a run nobody watched asked for - not applied until Arsen presses Apply. auto_reverted: the
# re-check failed and the old value is back. reverted / discarded / expired: undone, turned down,
# lapsed.
ChangeStatus = Literal["applied", "probing", "proposed", "auto_reverted", "reverted", "discarded", "expired"]


class ChangeField(BaseModel):
    """One field a change touched. A secret's values are always masked when sent anywhere."""

    path: str
    old: Any = None
    new: Any = None
    op: Literal["set", "add", "remove"] = "set"
    secret: bool = False


class ChangeRecord(BaseModel):
    id: str
    at: datetime
    area: ChangeArea
    #: "settings", a schedule id, or "PATCH /api/boards/b_1".
    target: str
    #: arsen (the UI, or a REST call) | jarvis (a tool in a run) | system | revert | probe-revert
    actor: str
    #: What a person would call where it came from: "chat", "Morning brief", "incognito chat".
    actor_label: str = ""
    run_id: str | None = None
    run_kind: str | None = None
    conversation_id: str | None = None
    call_id: str | None = None
    fields: list[ChangeField] = Field(default_factory=list)
    risk: list[str] = Field(default_factory=list)
    reason: str = ""
    status: ChangeStatus = "applied"
    #: The settings revision after this change (settings changes only).
    rev: int | None = None
    reverts: str | None = None
    reverted_by: str | None = None
    #: What the probe said, or what a revert cannot take back ("what it already did stays").
    note: str = ""
    revertible: bool = False


# --- collab -----------------------------------------------------------------------------


class CollabKey(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None = None


# --- meetings ---------------------------------------------------------------------------


class MeetingStatus(StrEnum):
    RECORDING = "recording"
    SUMMARISING = "summarising"
    DONE = "done"
    FAILED = "failed"


class Meeting(BaseModel):
    id: str
    conversation_id: str
    title: str
    host: str
    status: MeetingStatus = MeetingStatus.RECORDING
    started_at: datetime
    ended_at: datetime | None = None
    summary_run_id: str | None = None


class MeetingSegmentModel(BaseModel):
    seq: int
    t0: float
    t1: float
    text: str


class MeetingFrameModel(BaseModel):
    seq: int
    at: float
    url: str
    ocr: str | None = None


class MeetingDetail(Meeting):
    segments: list[MeetingSegmentModel] = Field(default_factory=list)
    frames: list[MeetingFrameModel] = Field(default_factory=list)


# --- triage -----------------------------------------------------------------------------


class TriageState(BaseModel):
    account: str
    cursor: str | None = None
    day: str | None = None
    processed_today: int = 0
    routed_today: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None
    # When mail last reached triage, while the account has been reported as stalled.
    stalled_since: datetime | None = None


# --- meeting auto-RSVP --------------------------------------------------------------------


class RsvpState(BaseModel):
    account: str
    last_run_at: datetime | None = None
    last_error: str | None = None
    answered_total: int = 0
    removed_canceled_total: int = 0


class RsvpDecision(BaseModel):
    """What the RSVP job did (or, on a dry run, would do) with one invite occurrence."""

    account: str
    subject: str
    organizer: str
    organizer_address: str = ""
    start: str
    end: str = ""
    decision: str  # accept | decline | accept_vip_conflict | left_external | failed
    detail: str = ""
    conflicts: list[str] = Field(default_factory=list)
    proposals: list[str] = Field(default_factory=list)
