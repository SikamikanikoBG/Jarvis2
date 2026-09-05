"""Runs, plans, conversations."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ThinkLevel = Literal["low", "medium", "high"]


class RunKind(StrEnum):
    CHAT = "chat"
    SCHEDULED = "scheduled"
    COLLAB = "collab"
    TRIAGE = "triage"
    MEETING = "meeting"
    SYSTEM = "system"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.DONE, RunStatus.FAILED, RunStatus.CANCELLED}


class RunBudget(BaseModel):
    max_steps: int = 25
    max_tokens: int = 200_000
    max_seconds: int = 600


class ModelUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    ttft_ms: int | None = None
    duration_ms: int = 0

    def add(self, other: ModelUsage) -> ModelUsage:
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
            ttft_ms=self.ttft_ms if self.ttft_ms is not None else other.ttft_ms,
            duration_ms=self.duration_ms + other.duration_ms,
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class PlanStepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    title: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    note: str | None = None


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep]

    @property
    def current_index(self) -> int | None:
        for i, s in enumerate(self.steps):
            if s.status in {PlanStepStatus.PENDING, PlanStepStatus.IN_PROGRESS}:
                return i
        return None


class Run(BaseModel):
    id: str
    conversation_id: str
    kind: RunKind
    status: RunStatus = RunStatus.QUEUED
    input_text: str = ""
    plan: Plan | None = None
    budget: RunBudget = Field(default_factory=RunBudget)
    priority: int = 0  # lower runs first; chat=0, collab=1, scheduled=2, triage=3
    steps_used: int = 0
    usage: ModelUsage = Field(default_factory=ModelUsage)
    last_seq: int = 0
    error: str | None = None
    waiting_reason: str | None = None
    # Per-run thinking override (None = use the role's setting).
    think: bool | None = None
    think_level: ThinkLevel | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ConversationKind(StrEnum):
    CHAT = "chat"
    SCHEDULED = "scheduled"
    COLLAB = "collab"
    TRIAGE = "triage"
    MEETING = "meeting"
    ARCHIVE = "archive"


class Conversation(BaseModel):
    id: str
    kind: ConversationKind = ConversationKind.CHAT
    title: str = "New chat"
    folder_key: str | None = None
    folder_label: str | None = None
    archived: bool = False
    unread: bool = False
    preview: str | None = None
    message_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
