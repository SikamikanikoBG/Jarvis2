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
    # How many prompt tokens the server answered from its prefix cache. Prefill is the whole of
    # TTFT on a long conversation and runs at a fixed ~1,200 tok/s here, so the only lever is how
    # much of the prompt has to be read again — and this is the server's own count of it, not an
    # inference from timings. 0 with a large prompt means the prefix changed.
    cached_tokens: int = 0

    def add(self, other: ModelUsage) -> ModelUsage:
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
            ttft_ms=self.ttft_ms if self.ttft_ms is not None else other.ttft_ms,
            duration_ms=self.duration_ms + other.duration_ms,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def prefilled_tokens(self) -> int:
        """Prompt tokens that actually had to be read: what TTFT is spent on."""
        return max(0, self.prompt_tokens - self.cached_tokens)

    @property
    def processed_tokens(self) -> int:
        """Work done: tokens the model actually read, plus the ones it wrote.

        Every step re-sends the whole conversation, so summing ``prompt_tokens`` over a run
        counts the same text once per step — a 20k conversation over 11 steps "costs" 220k
        without anything new being said. The prefix cache serves those repeats from KV, so they
        are neither time nor compute. This is the number a budget should be measured against;
        ``total_tokens`` stays what it says, for showing how big the context got.

        A provider that does not report ``cached_tokens`` leaves it at 0, and this equals
        ``total_tokens`` exactly as before.
        """
        return self.prefilled_tokens + self.completion_tokens


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


class ConversationActivity(StrEnum):
    """What the sidebar's activity dot says about a conversation.

    Derived from the runs table on every read, never stored: a run's status is the truth and a
    second copy of it on the conversation row would be the thing that goes stale.
    """

    IDLE = "idle"
    #: A run is queued or working. The dot pulses.
    RUNNING = "running"
    #: A run is parked on a confirmation and cannot move until Arsen answers.
    WAITING = "waiting"


class ChatFolder(BaseModel):
    """A folder Arsen made himself, to file plain chats in. See ``Conversation.folder_id``."""

    id: str
    name: str
    position: int = 0
    #: Chats filed here (unarchived), and how many of those are unread.
    conversation_count: int = 0
    unread_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Conversation(BaseModel):
    id: str
    kind: ConversationKind = ConversationKind.CHAT
    title: str = "New chat"
    folder_key: str | None = None
    folder_label: str | None = None
    # Arsen's own filing (a ChatFolder id), as opposed to folder_key/folder_label above, which
    # the machine fills in and which group the kind-folders.
    folder_id: str | None = None
    archived: bool = False
    unread: bool = False
    pinned: bool = False
    # True while the title is machine-made; a rename by the user turns it off for good.
    title_auto: bool = True
    # A persona or standing rule for THIS chat only, added to the system message. Empty for the
    # vast majority; when set it is what makes one conversation behave differently from the rest.
    instructions: str = ""
    preview: str | None = None
    message_count: int = 0
    # Derived from the runs table on read; see ConversationActivity.
    activity: ConversationActivity = ConversationActivity.IDLE
    # A private chat: nothing from it is remembered anywhere else. No knowledge is learned from
    # it, its title is never made from its words, search never returns it and the sidebar shows
    # no preview. Decided at creation and never switched on later - what an ordinary chat has
    # already taught the knowledge graph cannot be un-learned. Independent of the timer below:
    # an incognito chat stays until Arsen deletes it, unless he also gives it an idle time.
    incognito: bool = False
    # A disappearing chat: the core deletes it once it has sat idle this long. None = kept.
    # "Idle" is measured from the last message (updated_at), not from the last time it was read.
    ttl_seconds: int | None = None
    # updated_at + ttl_seconds, maintained by the store; what the sweeper reads.
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


#: The idle times a disappearing chat may be given, in seconds: an hour, a day, a week.
TTL_CHOICES: tuple[int, ...] = (3_600, 86_400, 604_800)
INCOGNITO_TITLE = "Incognito chat"


class SearchHit(BaseModel):
    """One search result: a conversation, and the message that matched when the text did."""

    conversation: Conversation
    message_id: str | None = None
    snippet: str | None = None
    matched: Literal["title", "message"] = "title"
