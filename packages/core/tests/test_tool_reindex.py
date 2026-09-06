"""A reconnected MCP server re-lists its tools by itself.

2026-09-06: the host gained meeting_start/pull/stop, the host restarted, and the core kept
answering "unknown tool 'workocholic.meeting_start'" until someone called /api/tools/reload by
hand. A tool set that only refreshes on a manual reload is a trap for every later host change.
"""

from __future__ import annotations

import asyncio

from jarvis_core.tools.registry import ToolRegistry
from jarvis_proto import ToolResult, ToolSpec


class FakeMcp:
    """A provider that can reconnect and change its tool set, like a restarted jarvis-host."""

    def __init__(self, name: str, tools: list[str]) -> None:
        self.name = name
        self.tools = tools
        self.error: str | None = None
        self.on_connect: object | None = None
        self.lists = 0
        self.fail_list = False

    async def list_tools(self) -> list[ToolSpec]:
        self.lists += 1
        if self.fail_list:
            raise RuntimeError("server went away mid-list")
        return [ToolSpec(name=f"{self.name}.{t}", description=t, input_schema={"type": "object"}) for t in self.tools]

    async def call(self, name: str, arguments: dict, *, cancel: asyncio.Event, idempotency_key: str, timeout_s: float) -> ToolResult:
        return ToolResult.data("ok")

    async def reconnect(self) -> None:
        hook = self.on_connect
        if hook is not None:
            await hook(self)  # type: ignore[operator]


async def test_a_reconnected_provider_replaces_its_own_tools_and_announces_it():
    host = FakeMcp("laptop", ["outlook_list"])
    other = FakeMcp("homelab", ["get_host"])
    registry = ToolRegistry()
    changed: list[str] = []
    registry.on_change(changed.append)
    registry.set_providers([host, other])
    await registry.refresh()
    assert sorted(s.name for s in registry.specs()) == ["homelab.get_host", "laptop.outlook_list"]

    # The host restarts with a new tool set and reconnects on its own.
    host.tools = ["outlook_list", "meeting_start"]
    await host.reconnect()
    assert sorted(s.name for s in registry.specs()) == [
        "homelab.get_host",
        "laptop.meeting_start",
        "laptop.outlook_list",
    ]
    assert changed == ["laptop"]  # the UI is told once, and only about what changed
    assert other.lists == 1  # the untouched provider was not re-listed

    # A tool that disappears is dropped, not left behind as an "unknown tool" surprise.
    host.tools = ["outlook_list"]
    await host.reconnect()
    assert [s.name for s in registry.specs() if s.name.startswith("laptop.")] == ["laptop.outlook_list"]
    assert changed == ["laptop", "laptop"]

    # An identical tool set is not announced: a reconnect is not news by itself.
    await host.reconnect()
    assert changed == ["laptop", "laptop"]


async def test_a_failed_relist_keeps_the_old_tools_and_records_why():
    host = FakeMcp("laptop", ["outlook_list"])
    registry = ToolRegistry([host])
    await registry.refresh()
    host.fail_list = True
    host.error = "connection reset"
    await host.reconnect()
    # Better the tools we knew than none: a transient failure must not empty the registry.
    assert [s.name for s in registry.specs()] == ["laptop.outlook_list"]
    health = {h["name"]: h for h in registry.provider_health()}
    assert health["laptop"]["ok"] is False and "connection reset" in health["laptop"]["error"]


async def test_reindexing_a_provider_that_was_replaced_does_nothing():
    old = FakeMcp("laptop", ["outlook_list"])
    new = FakeMcp("laptop", ["outlook_list", "meeting_start"])
    registry = ToolRegistry()
    registry.set_providers([old])
    await registry.refresh()
    registry.set_providers([new])  # settings changed; the old connection is on its way out
    await registry.refresh()
    await old.reconnect()  # the dying provider reconnects once more
    assert sorted(s.name for s in registry.specs()) == ["laptop.meeting_start", "laptop.outlook_list"]
