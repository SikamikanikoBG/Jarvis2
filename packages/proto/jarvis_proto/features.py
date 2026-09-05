"""Feature models shared by REST, tools and the web client (Phases 2–7)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

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
    next_fire: datetime | None = None
    last_fired_for: datetime | None = None
    last_run_id: str | None = None
    last_status: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScheduleFire(BaseModel):
    schedule_id: str
    scheduled_for: datetime
    run_id: str | None = None
    conversation_id: str | None = None
    status: str | None = None


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
