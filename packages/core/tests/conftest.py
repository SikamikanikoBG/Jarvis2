from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jarvis_core.app import Core
from jarvis_core.config import CoreConfig
from jarvis_core.engine.bus import Subscriber
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter
from jarvis_proto import ModelSpec, Provider, RoleName, Settings


def fake_settings() -> Settings:
    """Deterministic defaults for scripted-model tests: no MCP servers, no pre-flight/planner
    calls, no background learning — each test switches on exactly what it exercises."""
    spec = ModelSpec(provider=Provider.FAKE, base_url="fake://", model="fake")
    return Settings(
        roles={r: spec.model_copy(update={"think": r is RoleName.CHAT}) for r in RoleName},
        mcp_servers=[],
        planning_enabled=False,
        kg_learning=False,
        tool_exposure="flat",
    )


class Harness:
    """A fully wired Core on a temp DB with scripted fake models, no HTTP."""

    def __init__(self, tmp_path: Path) -> None:
        self.config = CoreConfig(home=tmp_path, token=None)
        self.core = Core(self.config)
        self.chat = FakeAdapter()
        self.judge = FakeAdapter()
        self.core.adapters.fakes = {r: self.chat for r in RoleName}
        self.core.adapters.fakes[RoleName.JUDGE] = self.judge

    async def start(self) -> None:
        await self.core.db.open()
        await self.core.store.save_settings(fake_settings())
        self.core.apply_settings(await self.core.store.load_settings())
        self.core.adapters.fakes = {r: self.chat for r in RoleName}
        self.core.adapters.fakes[RoleName.JUDGE] = self.judge
        await self.core.reload_tools()
        await self.core.engine.start()
        await self.core.scheduler.start()

    def enable(self, **fields: object) -> None:
        """Switch on a feature for one test (planning_enabled, kg_learning, ...)."""
        self.core.apply_settings(self.core.settings.model_copy(update=fields))

    async def stop(self) -> None:
        await self.core.stop()

    async def restart(self) -> None:
        """Simulate a process restart: shut down, rebuild every object, boot from the same DB."""
        await self.core.stop()
        reset_endpoint_semaphores()
        chat, judge = self.chat, self.judge
        self.core = Core(self.config)
        self.chat, self.judge = chat, judge
        await self.start()

    def subscribe(self, conversation_id: str) -> Subscriber:
        sub = Subscriber("test")
        sub.conversations.add(conversation_id)
        self.core.bus.attach(sub)
        return sub

    async def wait_for(self, sub: Subscriber, event_type: str, *, timeout: float = 5.0) -> list:
        """Drain ``sub`` until an event of ``event_type`` arrives; return everything seen."""
        seen: list = []
        async with asyncio.timeout(timeout):
            while True:
                ev = await sub.queue.get()
                seen.append(ev)
                if getattr(ev, "type", None) == event_type:
                    return seen


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    reset_endpoint_semaphores()
    h = Harness(tmp_path)
    await h.start()
    try:
        yield h
    finally:
        await h.stop()
