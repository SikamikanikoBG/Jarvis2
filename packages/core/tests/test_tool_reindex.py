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


async def test_a_dead_session_is_dropped_so_the_next_probe_reconnects():
    """The client only learns a server restarted when it uses the session. Holding on to it
    means every later call fails against the same corpse."""
    from jarvis_core.tools.mcp_provider import McpProvider
    from jarvis_proto import McpServerSpec, McpTransport

    spec = McpServerSpec(name="laptop", transport=McpTransport.STREAMABLE_HTTP, url="http://127.0.0.1:1/mcp")
    provider = McpProvider(spec)
    stopped: list[bool] = []

    class DeadSession:
        async def list_tools(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("Session not found")

    provider._session = DeadSession()  # type: ignore[assignment]
    original = provider.stop

    async def spy() -> None:
        stopped.append(True)
        await original()

    provider.stop = spy  # type: ignore[method-assign]
    try:
        await provider.list_tools()
    except RuntimeError:
        pass
    else:  # pragma: no cover - the fake always raises
        raise AssertionError("expected the dead session to raise")
    assert stopped == [True] and provider.connected is False
    assert provider.error and "Session not found" in provider.error


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


async def test_the_exposed_tool_list_is_ordered_by_name_not_by_registration_history():
    """The chat template renders this list into the system message, so its ORDER is part of the
    prompt prefix. `reindex` re-appends a provider's tools at the end, so a reconnect alone used
    to reorder the prompt and cost a full re-prefill of every cached conversation."""
    a = FakeMcp("alpha", ["two", "one"])
    b = FakeMcp("beta", ["four", "three"])
    registry = ToolRegistry()
    registry.set_providers([a, b])
    await registry.refresh()
    ordered = [s.name for s in registry.specs()]
    assert ordered == sorted(ordered) == ["alpha.one", "alpha.two", "beta.four", "beta.three"]

    # A reconnect of the FIRST provider must not move its tools behind the second one.
    await a.reconnect()
    assert [s.name for s in registry.specs()] == ordered
    # Nor must a reconnect that genuinely adds a tool disturb the rest of the ordering.
    a.tools = ["two", "one", "zero"]
    await a.reconnect()
    assert [s.name for s in registry.specs()] == ["alpha.one", "alpha.two", "alpha.zero", "beta.four", "beta.three"]


async def test_a_disconnected_provider_keeps_its_tools_in_the_prompt():
    """Closing a browser is not losing a capability.

    WsProvider answers with an empty list the moment the extension disconnects, and the tools
    left the system prompt — re-prefilling every conversation, measured at 17 s a turn. An empty
    listing from a provider that is REPORTING an error is an outage, not a change.
    """
    host = FakeMcp("laptop", ["outlook_list", "outlook_send"])
    registry = ToolRegistry([host])
    await registry.refresh()
    assert len(registry.specs()) == 2

    host.tools = []                       # what a disconnected WsProvider returns
    host.error = "browser extension not connected"
    await registry.refresh()
    assert [s.name for s in registry.specs()] == ["laptop.outlook_list", "laptop.outlook_send"]
    health = {h["name"]: h for h in registry.provider_health()}
    assert health["laptop"]["ok"] is False and "not connected" in health["laptop"]["error"]

    # Reconnecting with the same set changes nothing at all, and clears the error.
    host.tools, host.error = ["outlook_list", "outlook_send"], None
    await registry.refresh()
    assert [s.name for s in registry.specs()] == ["laptop.outlook_list", "laptop.outlook_send"]
    assert registry.provider_health()[0]["ok"] is True

    # A provider that really has no tools and no error contributes none: this is not a licence
    # to remember forever.
    host.tools = []
    await registry.refresh()
    assert registry.specs() == []

    # And a provider removed from settings takes its memory with it.
    host.tools, host.error = ["outlook_list"], None
    await registry.refresh()
    assert len(registry.specs()) == 1
    registry.set_providers([])
    await registry.refresh()
    assert registry.specs() == []


async def test_marking_a_provider_unavailable_keeps_the_tools_but_tells_the_truth():
    host = FakeMcp("browser", ["tabs"])
    registry = ToolRegistry([host])
    await registry.refresh()
    registry.mark_unavailable(host, "browser extension not connected")
    assert [s.name for s in registry.specs()] == ["browser.tabs"]
    health = registry.provider_health()[0]
    assert health["ok"] is False and "not connected" in health["error"]
