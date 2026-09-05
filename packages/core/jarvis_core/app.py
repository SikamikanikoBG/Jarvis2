"""Application wiring. ``Core`` owns every component; FastAPI is the transport."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from jarvis_core import __version__
from jarvis_core.api import collab, features, media, openai_compat, rest, ws
from jarvis_core.config import CoreConfig
from jarvis_core.db import Database, Store
from jarvis_core.engine import EventBus, RunEngine
from jarvis_core.engine.context import BoardsBlock, BrowserBlock, ContextAssembler, KnowledgeBlock, SkillsBlock
from jarvis_core.engine.loop import AgentLoop
from jarvis_core.engine.supervision import Supervisor
from jarvis_core.features.boards import BoardStore, NotesTools
from jarvis_core.features.collab import CollabAuthMiddleware, CollabKeys, build_mcp_server
from jarvis_core.features.compaction import Compactor
from jarvis_core.features.knowledge import KnowledgeLearner, KnowledgeStore, KnowledgeTools
from jarvis_core.features.meetings import MeetingService
from jarvis_core.features.planner import Planner
from jarvis_core.features.schedules import Scheduler, ScheduleStore, ScheduleTools
from jarvis_core.features.skills import SkillDetector, SkillsTools, SkillStore
from jarvis_core.features.stt import Transcriber
from jarvis_core.features.triage import TriageJob
from jarvis_core.models import AdapterFactory
from jarvis_core.tools import CoreTools, ToolRegistry
from jarvis_core.tools.facades import ExposurePolicy
from jarvis_core.tools.mcp_provider import McpProvider
from jarvis_core.tools.ws_provider import WsProvider
from jarvis_proto import ConversationKind, RunKind, Settings, ThinkLevel
from jarvis_proto.settings import RoleName

log = logging.getLogger(__name__)


class Core:
    def __init__(self, config: CoreConfig) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.store = Store(self.db)
        self.bus = EventBus()
        self.settings = Settings()
        self.adapters = AdapterFactory(self.settings)
        settings = lambda: self.settings  # noqa: E731 - late-bound accessor shared by every component

        # Features (each owns its tables, tools and context block).
        self.boards = BoardStore(self.db, self.bus)
        self.knowledge = KnowledgeStore(self.db, self.bus)
        self.skills = SkillStore(config.home / "skills", self.db, self.bus)
        self.skill_detector = SkillDetector(self.skills, lambda: self.adapters.for_role(RoleName.CLASSIFIER))
        self.compactor = Compactor(self.db, lambda: self.adapters.for_role(RoleName.CLASSIFIER))
        self.planner = Planner(
            lambda: self.adapters.for_role(RoleName.CLASSIFIER), lambda: self.adapters.for_role(RoleName.PLANNER)
        )
        self.learner = KnowledgeLearner(self.knowledge, lambda: self.adapters.for_role(RoleName.CLASSIFIER))
        self.schedules = ScheduleStore(self.db, self.bus)
        self.browser = WsProvider()
        self.collab_keys = CollabKeys(self.db)
        self.transcriber = Transcriber(settings)
        self.mcp_server = build_mcp_server(self)
        self.triage = TriageJob(self)
        self.meetings = MeetingService(self)

        # Tools.
        self.builtin = CoreTools()
        self.mcp: list[McpProvider] = []
        self.policy = ExposurePolicy(self.settings.tool_exposure, self.settings.facade_threshold)
        self.registry = ToolRegistry([self.builtin])

        # Engine.
        self.context = ContextAssembler(
            self.store,
            settings,
            providers=[
                BoardsBlock(self.boards, settings),
                SkillsBlock(self.skill_detector, settings),
                KnowledgeBlock(self.knowledge),
                BrowserBlock(self.browser),
            ],
            compactor=self.compactor,
        )
        self.supervisor = Supervisor(settings, lambda: self.adapters.for_role(RoleName.JUDGE))
        self.loop = AgentLoop(
            self.store,
            self.bus,
            self.adapters.for_role,
            self.registry,
            self.context,
            self.supervisor,
            settings,
            policy=self.policy,
            planner=self.planner,
            skills=self.skill_detector,
            learner=self.learner,
        )
        self.engine = RunEngine(self.store, self.bus, self.loop, settings, max_concurrent=config.max_concurrent_runs)
        self.scheduler = Scheduler(self.schedules, self._fire_schedule)

    async def _fire_schedule(
        self,
        *,
        text: str,
        conversation_kind: ConversationKind,
        folder_key: str,
        folder_label: str,
        title: str,
        kind: RunKind,
        think: bool | None,
        think_level: ThinkLevel | None,
    ) -> tuple[str, str]:
        conv = await self.store.create_conversation(
            kind=conversation_kind, title=title, folder_key=folder_key, folder_label=folder_label
        )
        run, conv = await self.engine.create_run(
            text=text, conversation_id=conv.id, kind=kind, think=think, think_level=think_level
        )
        return run.id, conv.id

    async def start(self) -> None:
        await self.db.open()
        self.apply_settings(await self.store.load_settings())
        await self.reload_tools()
        await self.engine.start()
        await self.scheduler.start()
        await self.triage.start()
        log.info("jarvis-core %s ready (db=%s, tools=%d)", __version__, self.db.path, len(self.registry.specs()))

    async def stop(self) -> None:
        await self.triage.stop()
        await self.meetings.stop_all()
        await self.scheduler.stop()
        await self.engine.stop()
        await self.learner.wait()
        for provider in self.mcp:
            await provider.stop()
        await self.transcriber.aclose()
        await self.db.close()

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.adapters.update_settings(settings)
        self.policy.mode = settings.tool_exposure
        self.policy.threshold = settings.facade_threshold

    async def reload_tools(self) -> None:
        """(Re)connect every enabled MCP server from settings and rebuild the tool index."""
        for provider in self.mcp:
            await provider.stop()
        self.mcp = [McpProvider(spec) for spec in self.settings.mcp_servers if spec.enabled]
        for provider in self.mcp:
            await provider.start()
        self.registry.set_providers(
            [
                self.builtin,
                NotesTools(self.boards),
                KnowledgeTools(self.knowledge),
                SkillsTools(self.skills),
                ScheduleTools(self.schedules, tz=lambda: self.settings.timezone),
                self.browser,
                *self.mcp,
            ]
        )
        await self.registry.refresh()


def create_app(config: CoreConfig | None = None, *, core: Core | None = None) -> FastAPI:
    cfg = config or CoreConfig()
    the_core = core or Core(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await the_core.start()
        try:
            async with the_core.mcp_server.session_manager.run():
                yield
        finally:
            await the_core.stop()

    app = FastAPI(
        title="Jarvis V2", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.state.core = the_core
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # A mounted sub-app only matches "/mcp/..."; MCP clients POST to "/mcp". Normalise before
    # routing so the exact path is not swallowed (405) by the SPA catch-all route.
    app.add_middleware(_TrailingSlashMiddleware, paths=("/mcp",))
    app.include_router(rest.open_router)
    app.include_router(rest.router)
    app.include_router(features.router)
    app.include_router(collab.owner_router)
    app.include_router(collab.router)
    app.include_router(media.router)
    app.include_router(openai_compat.router)
    app.include_router(ws.router)
    app.mount("/mcp", CollabAuthMiddleware(the_core.mcp_server.streamable_http_app(), the_core))
    _mount_spa(app, cfg.resolve_web_dist())
    return app


class _TrailingSlashMiddleware:
    def __init__(self, app: Any, paths: tuple[str, ...]) -> None:
        self.app = app
        self.paths = paths

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") in self.paths:
            scope = {**scope, "path": scope["path"] + "/", "raw_path": (scope["path"] + "/").encode()}
        await self.app(scope, receive, send)


def _mount_spa(app: FastAPI, dist: Path | None) -> None:
    if dist is None:

        @app.get("/", include_in_schema=False)
        async def no_ui() -> dict[str, str]:
            return {"message": "Jarvis V2 core is running; the web UI is not built (web/dist missing)."}

        return

    index = dist / "index.html"
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request) -> FileResponse:
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        response = FileResponse(index, headers={"Cache-Control": "no-cache"})
        # Opening the app with ?token=… also sets the cookie, so plain <img>/<a> requests
        # (meeting frames, downloads) authenticate without a header.
        core = request.app.state.core
        token = request.query_params.get("token")
        if token and core.config.token and token == core.config.token:
            response.set_cookie("jarvis_token", token, httponly=True, samesite="lax", max_age=365 * 24 * 3600)
        return response
