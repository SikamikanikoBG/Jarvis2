"""Settings — one typed document, one row per key in the DB, PATCHed by key.

Only what the core needs. Per-machine settings (audio device, Outlook profile) live on the
host that owns them and are not here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from jarvis_proto.runs import RunBudget, RunKind


class Provider(StrEnum):
    OLLAMA = "ollama"
    VLLM = "vllm"
    FAKE = "fake"  # scripted adapter for tests and CI


class RoleName(StrEnum):
    CHAT = "chat"
    PLANNER = "planner"
    CLASSIFIER = "classifier"
    JUDGE = "judge"
    TRIAGE = "triage"


class ModelSpec(BaseModel):
    provider: Provider = Provider.OLLAMA
    base_url: str = "http://localhost:11434"
    model: str = ""
    think: bool = False
    num_ctx: int | None = None
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout_s: int = 180
    keep_alive: str | None = None  # Ollama only, e.g. "30m"

    @property
    def endpoint_key(self) -> str:
        """Concurrency is per endpoint, not per role."""
        return f"{self.provider}@{self.base_url}"


def _default_roles() -> dict[RoleName, ModelSpec]:
    chat = ModelSpec(think=True)
    quiet = ModelSpec(think=False, temperature=0.1)
    return {
        RoleName.CHAT: chat,
        RoleName.PLANNER: quiet.model_copy(),
        RoleName.CLASSIFIER: quiet.model_copy(),
        RoleName.JUDGE: quiet.model_copy(),
        RoleName.TRIAGE: quiet.model_copy(),
    }


def _default_budgets() -> dict[RunKind, RunBudget]:
    return {
        RunKind.CHAT: RunBudget(max_steps=25, max_tokens=200_000, max_seconds=600),
        RunKind.COLLAB: RunBudget(max_steps=25, max_tokens=200_000, max_seconds=600),
        RunKind.SCHEDULED: RunBudget(max_steps=60, max_tokens=600_000, max_seconds=1800),
        RunKind.TRIAGE: RunBudget(max_steps=300, max_tokens=1_000_000, max_seconds=1800),
        RunKind.MEETING: RunBudget(max_steps=30, max_tokens=400_000, max_seconds=900),
        RunKind.SYSTEM: RunBudget(max_steps=10, max_tokens=50_000, max_seconds=120),
    }


class Settings(BaseModel):
    assistant_name: str = "Jarvis"
    user_name: str = "Arsen"
    timezone: str = "Europe/Sofia"
    language_hint: str = "Reply in the language the user wrote in (Bulgarian or English)."
    roles: dict[RoleName, ModelSpec] = Field(default_factory=_default_roles)
    budgets: dict[RunKind, RunBudget] = Field(default_factory=_default_budgets)
    max_concurrent_runs_per_endpoint: int = 1
    repeated_call_threshold: int = 3
    stt_url: str | None = None
    stt_languages: list[str] = Field(default_factory=lambda: ["bg", "en"])

    @model_validator(mode="after")
    def _only_chat_may_think(self) -> Settings:
        for role, spec in self.roles.items():
            if role is not RoleName.CHAT and spec.think:
                raise ValueError(
                    f"role {role.value!r} may not think: classifiers, planners and judges with "
                    "thinking on spend their whole budget thinking (V1 lesson, three times)."
                )
        return self
