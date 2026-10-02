"""The Office: Jarvis's work as pixel-art characters in a little office (the SPA's Office screen).

The office is pixel-agents' webview (https://github.com/pixel-agents-hq/pixel-agents, MIT), built
by ``web/office/build.sh`` and served at /pixel-office/; this module is the server side of its protocol, fed by the bus instead
of Claude Code hooks. One character per conversation with a run: it sits down at a desk when the
run starts, types while a tool runs (reads, for read-only tools), raises a flag while a tool waits
for Arsen's approval and shows the "done" bubble when the run ends. Background jobs that are not
runs (the mail triage pass) get a character of their own through ``job()``. A character that has
been idle for ``IDLE_CLOSE_S`` leaves.

Nothing here is the truth about a run — the runs table is. Agent ids are this process's own; a
restart rebuilds the office from the runs still open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis_core.engine.bus import Subscriber
from jarvis_proto.events import (
    ConversationDeleted,
    ConversationUpdated,
    RunCancelled,
    RunDone,
    RunFailed,
    RunInterrupted,
    RunQueued,
    RunResumed,
    RunStarted,
    RunSteered,
    RunWaitingUser,
    ToolCallEvent,
    ToolConfirmRequested,
    ToolConfirmResolved,
    ToolResultEvent,
)
from jarvis_proto.runs import RunStatus

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

IDLE_CLOSE_S = 600.0
REAP_EVERY_S = 30.0
CLIENT_QUEUE = 500
# The pixel-agents release the pinned build corresponds to. settingsLoaded reports it as both the
# current and the last-seen version, so the webview's "what's new" badge never asks about it.
PIXEL_AGENTS_VERSION = "1.4"
# A read-only tool reads at the desk; anything else types. readingTools names the read animation.
READ_TOOL = "Read"
WRITE_TOOL = "Write"
STATUS_MAX = 60

Send = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class OfficeAgent:
    id: int
    key: str  # a conversation id, or "job:<name>" for a background job
    title: str
    active: bool = False
    awaiting_input: bool = False
    permission: bool = False
    tools: dict[str, tuple[str, str]] = field(default_factory=dict)  # call_id -> (status, toolName)
    seat: dict[str, Any] = field(default_factory=dict)  # palette / hueShift / seatId, from the webview
    idle_since: float = field(default_factory=time.monotonic)

    @property
    def conversation_id(self) -> str | None:
        return None if self.key.startswith("job:") else self.key


def tool_status(name: str, arguments: dict[str, Any]) -> str:
    """What the label above a character says: the tool, and the first short text argument."""
    hint = next((v for v in arguments.values() if isinstance(v, str) and v.strip()), "")
    hint = " ".join(hint.split())
    text = f"{name} · {hint}" if hint else name
    return text if len(text) <= STATUS_MAX else text[: STATUS_MAX - 1] + "…"


class OfficeHub:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.agents: dict[str, OfficeAgent] = {}
        self._next_id = 1
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._boot: dict[str, Any] | None = None
        self._sub: Subscriber | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # --- files --------------------------------------------------------------------------

    @property
    def dist(self) -> Path | None:
        return self.core.config.resolve_office_dist()

    @property
    def _state_dir(self) -> Path:
        return self.core.config.home / "office"

    def _read_json(self, name: str) -> Any:
        with contextlib.suppress(OSError, ValueError):
            return json.loads((self._state_dir / name).read_text(encoding="utf-8"))
        return None

    def _write_json(self, name: str, value: Any) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_dir / f"{name}.tmp"
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(self._state_dir / name)

    def boot(self) -> dict[str, Any]:
        """The decoded sprites and default layout from the build (web/office/build.sh)."""
        if self._boot is not None:
            return self._boot
        dist = self.dist
        path = dist / "boot.json" if dist else None
        if path is None or not path.is_file():
            return {"assets": [], "defaultLayout": None}
        boot: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._boot = boot
        return boot

    # --- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        for run in await self.core.store.runs_with_status(RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_USER):
            agent = await self._ensure(run.conversation_id)
            agent.active = run.status is not RunStatus.WAITING_USER
            agent.permission = run.status is RunStatus.WAITING_USER
        self._sub = Subscriber("office", everything=True)
        self.core.bus.attach(self._sub)
        self._tasks = [
            asyncio.create_task(self._consume(self._sub), name="office-events"),
            asyncio.create_task(self._reap(), name="office-reaper"),
        ]

    async def stop(self) -> None:
        if self._sub is not None:
            self.core.bus.detach(self._sub)
            self._sub = None
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _consume(self, sub: Subscriber) -> None:
        while True:
            event = await sub.queue.get()
            try:
                await self.on_event(event)
            except Exception:
                log.exception("office: %s not shown", getattr(event, "type", type(event).__name__))

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(REAP_EVERY_S)
            self.reap()

    def reap(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for agent in list(self.agents.values()):
            idle = not agent.active and not agent.permission and not agent.tools
            if idle and now - agent.idle_since >= IDLE_CLOSE_S:
                self._remove(agent)

    # --- agents -------------------------------------------------------------------------

    def by_id(self, agent_id: int) -> OfficeAgent | None:
        return next((a for a in self.agents.values() if a.id == agent_id), None)

    async def _ensure(self, key: str, title: str | None = None) -> OfficeAgent:
        agent = self.agents.get(key)
        if agent is not None:
            return agent
        if title is None:
            conv = await self.core.store.get_conversation(key)
            title = conv.title if conv else "Jarvis"
        agent = OfficeAgent(id=self._next_id, key=key, title=title)
        self._next_id += 1
        self.agents[key] = agent
        self._broadcast({"type": "agentCreated", "id": agent.id, "folderName": agent.title})
        return agent

    def _remove(self, agent: OfficeAgent) -> None:
        if self.agents.pop(agent.key, None) is not None:
            self._broadcast({"type": "agentClosed", "id": agent.id})

    def _set_active(self, agent: OfficeAgent) -> None:
        if not agent.active or agent.awaiting_input:
            agent.active, agent.awaiting_input = True, False
            self._broadcast({"type": "agentStatus", "id": agent.id, "status": "active"})

    def _set_done(self, agent: OfficeAgent, *, awaiting_input: bool = False) -> None:
        agent.tools.clear()
        agent.active, agent.permission, agent.awaiting_input = False, False, awaiting_input
        agent.idle_since = time.monotonic()
        self._broadcast({"type": "agentToolsClear", "id": agent.id})
        self._broadcast({"type": "agentStatus", "id": agent.id, "status": "waiting", "awaitingInput": awaiting_input})

    def _tool_start(self, agent: OfficeAgent, call_id: str, status: str, tool_name: str) -> None:
        self._set_active(agent)
        agent.tools[call_id] = (status, tool_name)
        self._broadcast(
            {"type": "agentToolStart", "id": agent.id, "toolId": call_id, "status": status, "toolName": tool_name}
        )

    def _tool_done(self, agent: OfficeAgent, call_id: str) -> None:
        if agent.tools.pop(call_id, None) is not None:
            self._broadcast({"type": "agentToolDone", "id": agent.id, "toolId": call_id})

    async def on_event(self, event: Any) -> None:
        if isinstance(event, ConversationDeleted):
            if (agent := self.agents.get(event.conversation_id)) is not None:
                self._remove(agent)
            return
        if isinstance(event, ConversationUpdated):
            # A label is fixed once the character exists; a new title shows from the next connect.
            if (agent := self.agents.get(event.conversation.id)) is not None:
                agent.title = event.conversation.title
            return
        if isinstance(event, RunQueued | RunStarted | RunResumed | RunSteered):
            self._set_active(await self._ensure(event.conversation_id))
        elif isinstance(event, ToolCallEvent):
            agent = await self._ensure(event.conversation_id)
            tool = READ_TOOL if event.read_only else WRITE_TOOL
            self._tool_start(agent, event.call_id, tool_status(event.name, event.arguments), tool)
        elif isinstance(event, ToolResultEvent):
            if (agent := self.agents.get(event.conversation_id)) is not None:
                self._tool_done(agent, event.call_id)
        elif isinstance(event, ToolConfirmRequested):
            agent = await self._ensure(event.conversation_id)
            agent.permission = True
            self._broadcast({"type": "agentToolPermission", "id": agent.id})
        elif isinstance(event, ToolConfirmResolved):
            if (agent := self.agents.get(event.conversation_id)) is not None and agent.permission:
                agent.permission = False
                self._broadcast({"type": "agentToolPermissionClear", "id": agent.id})
        elif isinstance(event, RunWaitingUser):
            # A confirmation already raised the approval flag; anything else asks Arsen to reply.
            if event.call_id is None and (agent := self.agents.get(event.conversation_id)) is not None:
                self._set_done(agent, awaiting_input=True)
        elif isinstance(event, RunDone | RunFailed | RunCancelled | RunInterrupted) and (
            agent := self.agents.get(event.conversation_id)
        ):
            self._set_done(agent)

    @contextlib.asynccontextmanager
    async def job(self, name: str, title: str, status: str | None = None) -> AsyncGenerator[None]:
        """A background job that is not a run (the mail triage pass), shown as its own character."""
        try:
            agent = await self._ensure(f"job:{name}", title)
            self._tool_start(agent, f"job:{name}", status or title, WRITE_TOOL)
        except Exception:
            log.exception("office: job %s not shown", name)
            agent = None
        try:
            yield
        finally:
            if agent is not None and agent.key in self.agents:
                self._set_done(agent)

    def view(self) -> list[dict[str, Any]]:
        """For GET /api/office/agents: which character is which conversation."""
        return [
            {
                "id": a.id,
                "conversation_id": a.conversation_id,
                "title": a.title,
                "active": a.active,
                "permission": a.permission,
                "awaiting_input": a.awaiting_input,
                "tools": [status for status, _ in a.tools.values()],
            }
            for a in sorted(self.agents.values(), key=lambda a: a.id)
        ]

    # --- clients ------------------------------------------------------------------------

    def connect(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self._clients.add(queue)
        return queue

    def disconnect(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._clients.discard(queue)

    def connected(self, queue: asyncio.Queue[dict[str, Any]]) -> bool:
        return queue in self._clients

    def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Its socket closes (api/office.py) and the webview reconnects to a fresh handshake.
                self._clients.discard(queue)
                log.warning("office: client dropped (queue full)")

    async def handle(self, message: dict[str, Any], send: Send) -> None:
        """One message from a webview. Anything the office does not do here is ignored."""
        kind = message.get("type")
        if kind == "webviewReady":
            await self._handshake(send)
        elif kind == "saveLayout" and isinstance(message.get("layout"), dict):
            self._write_json("layout.json", message["layout"])
        elif kind == "saveAgentSeats" and isinstance(message.get("seats"), dict):
            for raw_id, seat in message["seats"].items():
                agent = self.by_id(int(raw_id)) if str(raw_id).isdigit() else None
                if agent is not None and isinstance(seat, dict):
                    agent.seat = {k: seat[k] for k in ("palette", "hueShift", "seatId") if k in seat}
        elif kind == "closeAgent" and isinstance(message.get("id"), int):
            if (agent := self.by_id(message["id"])) is not None:
                self._remove(agent)
        elif kind in {"setSoundEnabled", "setAlwaysShowLabels", "setLastSeenVersion", "setShowAreas"}:
            settings = self._read_json("settings.json") or {}
            settings[kind] = message.get("enabled", message.get("version"))
            self._write_json("settings.json", settings)
        elif kind == "saveAreaMappings" and isinstance(message.get("mappings"), dict):
            settings = self._read_json("settings.json") or {}
            settings["areaMappings"] = message["mappings"]
            self._write_json("settings.json", settings)

    async def _handshake(self, send: Send) -> None:
        """The order pixel-agents' own server uses: capabilities, sprites, settings, the agents,
        then the layout (which turns the buffered agents into characters), then their activity."""
        boot = self.boot()
        settings = self._read_json("settings.json") or {}
        await send({"type": "providerCapabilities", "readingTools": [READ_TOOL], "subagentToolNames": []})
        for message in boot.get("assets", []):
            await send(message)
        await send(
            {
                "type": "settingsLoaded",
                "soundEnabled": bool(settings.get("setSoundEnabled", False)),
                "lastSeenVersion": PIXEL_AGENTS_VERSION,
                "extensionVersion": PIXEL_AGENTS_VERSION,
                "watchAllSessions": False,
                "alwaysShowLabels": bool(settings.get("setAlwaysShowLabels", True)),
                "ghostHeadlessAgents": False,
                "hooksEnabled": False,
                "hooksInfoShown": True,
                "externalAssetDirectories": [],
                "showAreas": bool(settings.get("setShowAreas", False)),
            }
        )
        await send({"type": "areaMappingsLoaded", "mappings": settings.get("areaMappings", {})})
        agents = sorted(self.agents.values(), key=lambda a: a.id)
        await send(
            {
                "type": "existingAgents",
                "agents": [a.id for a in agents],
                "agentMeta": {str(a.id): a.seat for a in agents},
                "folderNames": {str(a.id): a.title for a in agents},
                "externalAgents": {},
            }
        )
        await send({"type": "layoutLoaded", "layout": self._read_json("layout.json") or boot.get("defaultLayout")})
        for a in agents:
            if a.active:
                await send({"type": "agentStatus", "id": a.id, "status": "active"})
            for call_id, (status, tool_name) in a.tools.items():
                await send(
                    {"type": "agentToolStart", "id": a.id, "toolId": call_id, "status": status, "toolName": tool_name}
                )
            if a.permission:
                await send({"type": "agentToolPermission", "id": a.id})
            elif not a.active:
                await send({"type": "agentStatus", "id": a.id, "status": "waiting", "awaitingInput": a.awaiting_input})
