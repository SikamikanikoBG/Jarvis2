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
        # The AI Newsletter (14 mails + 8 searches + 13 pages) legitimately needs ~650k tokens.
        RunKind.SCHEDULED: RunBudget(max_steps=90, max_tokens=1_200_000, max_seconds=3600),
        RunKind.TRIAGE: RunBudget(max_steps=300, max_tokens=1_000_000, max_seconds=1800),
        RunKind.MEETING: RunBudget(max_steps=30, max_tokens=400_000, max_seconds=900),
        RunKind.SYSTEM: RunBudget(max_steps=10, max_tokens=50_000, max_seconds=120),
    }


def _default_mcp_servers() -> list[McpServerSpec]:
    return [
        McpServerSpec(name="fetch", transport=McpTransport.STDIO, command="{python}", args=["-m", "mcp_server_fetch"]),
        McpServerSpec(name="homelab", transport=McpTransport.STREAMABLE_HTTP, url="http://ardi:9810/mcp"),
    ]


_AUTO_REPLY_PREFIXES = (
    "automatic reply:",
    "auto-reply:",
    "autoreply:",
    "out of office",
    "автоматичен отговор",
    "отсъствие",
    "accepted:",
    "declined:",
    "tentative:",
    "undeliverable:",
)


def is_auto_reply(subject: str) -> bool:
    """A bounce, an out-of-office or a meeting response - the sender's mail system talking, not
    the sender. Prefix matching only: a subject that MENTIONS an out-of-office is a real mail."""
    low = " ".join(subject.split()).lower()
    for marker in ("re:", "fw:", "fwd:", "отн:", "препр:"):
        while low.startswith(marker):
            low = low[len(marker) :].lstrip()
    return low.startswith(_AUTO_REPLY_PREFIXES)


class TriageAlert(BaseModel):
    """"Tell me the moment this person writes." Ported from V1's alerts_config.json.

    The match is structural (an address, a domain, a subject substring) - never a judgement -
    so a VIP mail cannot be missed because a classifier had an opinion. An alert does not change
    where the mail is filed; it only makes Arsen's phone buzz.
    """

    name: str = ""
    enabled: bool = True
    # Full addresses ("pdimitrova@postbank.bg") or whole domains ("@board.bg"); empty = any sender.
    senders: list[str] = Field(default_factory=list)
    # Case-insensitive substrings of the subject; empty = any subject.
    keywords: list[str] = Field(default_factory=list)
    # An out-of-office bounce from a VIP is still not the VIP writing to you. Measured on the real
    # mailbox: 1 of 4 alerts was "Automatic reply: ...". Off only if you really want those buzzes.
    skip_auto_replies: bool = True

    def matches(self, address: str, display_name: str, subject: str) -> bool:
        if not self.enabled or (not self.senders and not self.keywords):
            return False
        if self.skip_auto_replies and is_auto_reply(subject):
            return False
        if self.senders:
            addr = address.strip().lower()
            name = display_name.strip().lower()
            domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
            hit = False
            for raw in self.senders:
                want = raw.strip().lower().lstrip("@")
                if not want:
                    continue
                if addr == want or (domain and domain == want) or (want in name and "@" not in want):
                    hit = True
                    break
            if not hit:
                return False
        if self.keywords:
            low = subject.lower()
            return any(k.strip().lower() in low for k in self.keywords if k.strip())
        return True


class TriageRules(BaseModel):
    """How one mailbox is sorted: its categories, the free-text rules, the catch-all.

    ``categories`` entries are ``{name, folder, rule}``; folders are paths under the account's
    inbox (``Action Hub/To-Do``) or a well-known role (``deleted`` for spam). ``instructions`` is
    read before the category list (who the owner is, what Cc-only means, VIPs, hard exclusions).
    ``fallback_category`` receives "none": empty leaves the mail in the inbox.
    """

    categories: list[dict[str, str]] = Field(default_factory=list)
    instructions: str = ""
    fallback_category: str = ""
    # DM-1234 → Demands/DM-1234 makes sense for the work mailbox, not for a personal Gmail.
    demand_routing: bool = True
    # Senders (and subjects) worth a push notification the moment they arrive.
    alerts: list[TriageAlert] = Field(default_factory=list)

    def folders(self) -> list[str]:
        return [str(c["folder"]) for c in self.categories if c.get("folder")]

    def alert_for(self, address: str, display_name: str, subject: str) -> str | None:
        """The name of the first alert this mail trips, or None."""
        for alert in self.alerts:
            if alert.matches(address, display_name, subject):
                return alert.name or "alert"
        return None


class TriageSettings(BaseModel):
    enabled: bool = False
    interval_min: int = 15
    host: str = ""  # name of the MCP server (jarvis-host) that owns Outlook
    accounts: list[str] = Field(default_factory=list)
    demand_root: str = "Demands"
    demand_prefixes: list[str] = Field(default_factory=lambda: ["DM-"])
    # The DEFAULT rules (every account without an override).
    categories: list[dict[str, str]] = Field(default_factory=list)  # {name, folder, rule}
    instructions: str = ""
    fallback_category: str = ""
    alerts: list[TriageAlert] = Field(default_factory=list)
    # Per-account overrides, keyed by the account name `outlook_accounts` reports (case-insensitive).
    # The work mailbox and a personal Gmail want different folders and different rules.
    account_rules: dict[str, TriageRules] = Field(default_factory=dict)

    def rules_for(self, account: str) -> TriageRules:
        wanted = account.strip().lower()
        for name, rules in self.account_rules.items():
            if name.strip().lower() == wanted:
                return rules
        return TriageRules(
            categories=self.categories,
            instructions=self.instructions,
            fallback_category=self.fallback_category,
            alerts=self.alerts,
        )


class MeetingRsvpSettings(BaseModel):
    """Answer meeting invites automatically through the host's calendar (docs/stories/08).

    Free slot → accept. Clash with a committed meeting → decline and propose free alternatives.
    A VIP is never declined (accept + tell Arsen). Outside ``allowed_domains`` nothing is
    answered — an empty list therefore answers nobody, which is the safe default.
    """

    enabled: bool = False
    interval_min: int = 3
    host: str = ""  # jarvis-host MCP server that owns the calendar
    account: str = ""  # Outlook account (store) whose invites are answered; "" = default
    lookahead_days: int = 5
    allowed_domains: list[str] = Field(default_factory=list)  # e.g. ["postbank.bg"]
    vip: list[str] = Field(default_factory=list)  # addresses or domains, never declined
    remove_canceled: bool = True
    propose_slots: int = 3
    work_start_hour: int = 9
    work_end_hour: int = 18

    @staticmethod
    def _domain(address: str) -> str:
        address = address.strip().lower()
        return address.rsplit("@", 1)[-1] if "@" in address else ""

    def is_vip(self, address: str) -> bool:
        a = address.strip().lower()
        d = self._domain(a)
        vips = {v.strip().lower().lstrip("@") for v in self.vip if v.strip()}
        return bool(a) and (a in vips or (bool(d) and d in vips))

    def is_allowed(self, address: str) -> bool:
        """Organizer inside the organisation (or a VIP). Unknown/empty address → not allowed."""
        d = self._domain(address)
        allowed = {x.strip().lower().lstrip("@") for x in self.allowed_domains if x.strip()}
        return self.is_vip(address) or (bool(d) and d in allowed)


class EmailPolicy(BaseModel):
    """Who Jarvis may send to directly. Everyone else gets a draft in Outlook.

    Entries are full addresses (``rumen@bank.bg``) or whole domains (``@bank.bg``), matched
    case-insensitively. An empty list means nothing goes out automatically — every message is
    drafted, which is the safe default for a fresh install.
    """

    approved_direct_send: list[str] = Field(default_factory=list)
    # Turn the whole policy off and let the model send to anyone. Off by design.
    allow_any_recipient: bool = False

    @staticmethod
    def split_recipients(*fields: str) -> list[str]:
        out: list[str] = []
        for field in fields:
            for part in str(field or "").replace(";", ",").split(","):
                if addr := part.strip():
                    out.append(addr)
        return out

    def is_approved(self, recipient: str) -> bool:
        # Take the address out of "Name <addr@host>" when it is there.
        addr = recipient.strip().lower()
        if "<" in addr and ">" in addr:
            addr = addr[addr.rfind("<") + 1 : addr.rfind(">")].strip()
        if not addr:
            return False
        for entry in self.approved_direct_send:
            e = entry.strip().lower()
            if not e:
                continue
            if e.startswith("@") and addr.endswith(e):
                return True
            if e == addr:
                return True
        return False

    def unapproved(self, *fields: str) -> list[str]:
        """Recipients that may NOT be written to directly (empty = safe to send)."""
        if self.allow_any_recipient:
            return []
        return [r for r in self.split_recipients(*fields) if not self.is_approved(r)]


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
    email: EmailPolicy = Field(default_factory=EmailPolicy)
    roles: dict[RoleName, ModelSpec] = Field(default_factory=_default_roles)
    budgets: dict[RunKind, RunBudget] = Field(default_factory=_default_budgets)
    mcp_servers: list[McpServerSpec] = Field(default_factory=_default_mcp_servers)
    # Two, because pre-flight asks the model two independent questions about the incoming message
    # (which skills apply, and whether it needs a plan) and they now go together — at 1 they
    # queue behind each other and the gather is a no-op. Measured on ardi 2026-09-06: median time
    # to Arsen's first token 1,181 ms at 1, 992 ms at 2, 803 ms at 3. It stops at 2 on purpose:
    # above that, several long runs can be in flight at once and a scheduled run reaches ~95k
    # tokens against a 305k-token KV cache, so they start evicting each other's cached prefixes —
    # which is the expensive thing this whole exercise removed. Raise it if the box grows.
    max_concurrent_runs_per_endpoint: int = 2
    repeated_call_threshold: int = 3
    # Tool exposure: "facade" = one tool per namespace with an op enum (derived from the live
    # list); "flat" = every tool; "auto" = facades once more than facade_threshold tools exist.
    # Measured 2026-09-05 with qwen3.8-27b: flat won (1 call / 9.5k tokens vs 7 calls / 12.1k
    # with a 20-op facade). Keep "flat" until a larger sample says otherwise.
    tool_exposure: Literal["auto", "flat", "facade"] = "flat"
    facade_threshold: int = 24
    history_token_budget: int = 24_000
    # Tool results accumulated within ONE run may occupy this much before the oldest are
    # truncated to a head. Large enough for "read 26 mails and summarise"; small enough that a
    # 49-event calendar does not ride along whole in every one of 8 model calls.
    tool_context_token_budget: int = 40_000
    # Ceiling for a single tool call. A tool that asks for its own timeout_s (shell_run running a
    # long report) is honoured up to this; everything else gets the 120 s default.
    tool_timeout_max_s: int = 1200  # Arsen's Outlook workload report takes ~14 min
    boards_context_chars: int = 6_000
    skill_max_chars: int = 6_000
    planning_enabled: bool = True
    kg_learning: bool = True
    triage: TriageSettings = Field(default_factory=TriageSettings)
    rsvp: MeetingRsvpSettings = Field(default_factory=MeetingRsvpSettings)
    # The address people actually reach this core on (e.g. the Tailscale Serve HTTPS URL).
    # Used for pairing/QR; without it the URL is derived from the request. The phone's mic and
    # the PWA need a secure context, so this is normally an https:// URL.
    public_url: str | None = None
    # The push channel for reminders and scheduled-run summaries (notify.discord). Secret:
    # never echoed in logs or the UI beyond "configured".
    discord_webhook_url: str | None = None
    # SearXNG base URL for web.search (ardi runs one). None = no search tool.
    searxng_url: str | None = None
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
