"""Application wiring. ``Core`` owns every component; FastAPI is the transport."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from jarvis_core import __version__
from jarvis_core.api import rest, ws
from jarvis_core.config import CoreConfig
from jarvis_core.db import Database, Store
from jarvis_core.engine import EventBus, RunEngine
from jarvis_core.engine.context import ContextAssembler
from jarvis_core.engine.loop import AgentLoop
from jarvis_core.engine.supervision import Supervisor
from jarvis_core.models import AdapterFactory
from jarvis_core.tools import BuiltinProvider, ToolRegistry
from jarvis_proto import Settings
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
        self.registry = ToolRegistry([BuiltinProvider()])
        self.context = ContextAssembler(self.store, lambda: self.settings)
        self.supervisor = Supervisor(lambda: self.settings, lambda: self.adapters.for_role(RoleName.JUDGE))
        self.loop = AgentLoop(
            self.store,
            self.bus,
            self.adapters.for_role,
            self.registry,
            self.context,
            self.supervisor,
            lambda: self.settings,
        )
        self.engine = RunEngine(
            self.store, self.bus, self.loop, lambda: self.settings, max_concurrent=config.max_concurrent_runs
        )

    async def start(self) -> None:
        await self.db.open()
        self.apply_settings(await self.store.load_settings())
        await self.registry.refresh()
        await self.engine.start()
        log.info("jarvis-core %s ready (db=%s, tools=%d)", __version__, self.db.path, len(self.registry.specs()))

    async def stop(self) -> None:
        await self.engine.stop()
        await self.db.close()

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.adapters.update_settings(settings)


def create_app(config: CoreConfig | None = None, *, core: Core | None = None) -> FastAPI:
    cfg = config or CoreConfig()
    the_core = core or Core(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await the_core.start()
        try:
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
    app.include_router(rest.router)
    app.include_router(ws.router)
    _mount_spa(app, cfg.resolve_web_dist())
    return app


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
    async def spa(path: str) -> FileResponse:
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
