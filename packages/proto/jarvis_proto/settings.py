"""Settings — one typed document, one row per key in the DB, PATCHed by key.

Only what the core needs. Per-machine settings (audio device, Outlook profile) live on the
host that owns them and are not here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from jarvis_proto.runs import RunBudget, RunKind, ThinkLevel


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
    # Only meaningful when ``think`` is on. Ollama: sent as the string level for models that
    # support levels (gpt-oss); vLLM: ``reasoning_effort``. None = the model's default depth.
    think_level: ThinkLevel | None = None
    num_ctx: int | None = None
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout_s: int = 180
    keep_alive: str | None = None  # Ollama only, e.g. "30m"

    @model_validator(mode="after")
    def _level_needs_think(self) -> ModelSpec:
        if not self.think and self.think_level is not None:
            object.__setattr__(self, "think_level", None)
        return self

    @property
    def endpoint_key(self) -> str:
        """Concurrency is per endpoint, not per role."""
        return f"{self.provider}@{self.base_url}"

    def with_thinking(self, think: bool | None, level: ThinkLevel | None) -> ModelSpec:
        """A copy with a per-run override applied; ``None`` keeps the configured value."""
        if think is None and level is None:
            return self
        on = self.think if think is None else think
        lvl = level if level is not None else self.think_level
        return self.model_copy(update={"think": on, "think_level": lvl if on else None})


class McpTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpServerSpec(BaseModel):
    """An external MCP server whose tools appear as ``<name>.<tool>``.

    ``command`` may be the literal ``{python}``, resolved to the core's own interpreter at
    spawn time, so the default ``fetch`` server works on any machine and inside Docker.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    transport: McpTransport = McpTransport.STDIO
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    timeout_s: int = 60

    @model_validator(mode="after")
    def _transport_fields(self) -> McpServerSpec:
        if self.transport is McpTransport.STDIO and not self.command:
            raise ValueError(f"mcp server {self.name!r}: stdio transport needs a command")
        if self.transport is McpTransport.STREAMABLE_HTTP and not self.url:
            raise ValueError(f"mcp server {self.name!r}: streamable_http transport needs a url")
        return self


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


def _default_mcp_servers() -> list[McpServerSpec]:
    return [
        McpServerSpec(name="fetch", transport=McpTransport.STDIO, command="{python}", args=["-m", "mcp_server_fetch"]),
        McpServerSpec(name="homelab", transport=McpTransport.STREAMABLE_HTTP, url="http://ardi:9810/mcp"),
    ]


class TriageSettings(BaseModel):
    enabled: bool = False
    interval_min: int = 15
    host: str = ""  # name of the MCP server (jarvis-host) that owns Outlook
    accounts: list[str] = Field(default_factory=list)
    demand_root: str = "Demands"
    demand_prefixes: list[str] = Field(default_factory=lambda: ["DM-"])
    categories: list[dict[str, str]] = Field(default_factory=list)  # {name, folder, rule}


class Confirmations(BaseModel):
    """When Jarvis stops to ask before running a tool.

    ``destructive`` (default) asks before anything a tool declares destructive — sending mail,
    creating calendar entries, writing files, running a shell command. ``off`` never asks: full
    autonomy, and the supervisor plus per-run budgets are what keep a bad idea bounded.

    Unattended runs (scheduled, triage, meeting, system) never ask regardless — a schedule that
    fires at 06:30 has nobody to answer it.
    """

    mode: Literal["destructive", "off"] = "destructive"
    # Tool names or `namespace.*` patterns that never ask, whatever the mode. Use it to free the
    # tools you trust (e.g. "workocholic.shell_run") while mail still stops for a look.
    always_allow: list[str] = Field(default_factory=list)
    # Names that ALWAYS ask, even with mode="off". The last line before something irreversible.
    always_ask: list[str] = Field(default_factory=lambda: ["*.outlook_send"])

    @staticmethod
    def _matches(name: str, pattern: str) -> bool:
        if pattern in (name, "*"):
            return True
        if pattern.endswith(".*"):
            return name.startswith(pattern[:-1])
        if pattern.startswith("*."):
            return name.split(".", 1)[-1] == pattern[2:]
        return False

    def needs_confirmation(self, name: str, *, destructive: bool, unattended: bool) -> bool:
        if any(self._matches(name, p) for p in self.always_ask):
            return not unattended
        if unattended or self.mode == "off":
            return False
        if any(self._matches(name, p) for p in self.always_allow):
            return False
        return destructive


class Personality(BaseModel):
    """How Jarvis speaks. Tone only — it may never change what he is willing to say."""

    enabled: bool = True
    formality: Literal["formal", "balanced", "casual"] = "casual"
    humor: Literal["none", "light", "witty"] = "witty"
    verbosity: Literal["terse", "concise", "detailed"] = "concise"
    address_style: Literal["name", "sir", "neutral"] = "name"
    persona: str = (
        "Dry, quick and unimpressed by hype. You have opinions and you state them in one line. "
        "You never pad, never flatter, and never explain what Arsen already knows."
    )


class Settings(BaseModel):
    assistant_name: str = "Jarvis"
    user_name: str = "Arsen"
    timezone: str = "Europe/Sofia"
    language_hint: str = "Reply in the language the user wrote in (Bulgarian or English)."
    personality: Personality = Field(default_factory=Personality)
    confirmations: Confirmations = Field(default_factory=Confirmations)
    roles: dict[RoleName, ModelSpec] = Field(default_factory=_default_roles)
    budgets: dict[RunKind, RunBudget] = Field(default_factory=_default_budgets)
    mcp_servers: list[McpServerSpec] = Field(default_factory=_default_mcp_servers)
    max_concurrent_runs_per_endpoint: int = 1
    repeated_call_threshold: int = 3
    # Tool exposure: "facade" = one tool per namespace with an op enum (derived from the live
    # list); "flat" = every tool; "auto" = facades once more than facade_threshold tools exist.
    # Measured 2026-09-05 with qwen3.8-27b: flat won (1 call / 9.5k tokens vs 7 calls / 12.1k
    # with a 20-op facade). Keep "flat" until a larger sample says otherwise.
    tool_exposure: Literal["auto", "flat", "facade"] = "flat"
    facade_threshold: int = 24
    history_token_budget: int = 24_000
    boards_context_chars: int = 6_000
    skill_max_chars: int = 6_000
    planning_enabled: bool = True
    kg_learning: bool = True
    triage: TriageSettings = Field(default_factory=TriageSettings)
    # The address people actually reach this core on (e.g. the Tailscale Serve HTTPS URL).
    # Used for pairing/QR; without it the URL is derived from the request. The phone's mic and
    # the PWA need a secure context, so this is normally an https:// URL.
    public_url: str | None = None
    stt_url: str | None = None
    stt_kind: Literal["openai", "asr"] = "openai"  # OpenAI-compatible /v1/audio/transcriptions or WhisperX /asr
    stt_model: str = "large-v3"
    stt_languages: list[str] = Field(default_factory=lambda: ["bg", "en"])

    @model_validator(mode="after")
    def _only_chat_may_think(self) -> Settings:
        for role, spec in self.roles.items():
            if role is not RoleName.CHAT and spec.think:
                raise ValueError(
                    f"role {role.value!r} may not think: classifiers, planners and judges with "
                    "thinking on spend their whole budget thinking (V1 lesson, three times)."
                )
        names = [s.name for s in self.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("mcp server names must be unique")
        if "jarvis" in names:
            raise ValueError("'jarvis' is reserved for built-in tools")
        return self
