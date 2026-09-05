"""McpProvider against a real stdio MCP server (a subprocess), and through the loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools.mcp_provider import McpProvider
from jarvis_proto import McpServerSpec, McpTransport, ToolCall, ToolResultKind
from tests.conftest import Harness

SERVER = Path(__file__).with_name("mcp_echo_server.py")


def echo_spec(name: str = "echo") -> McpServerSpec:
    return McpServerSpec(name=name, transport=McpTransport.STDIO, command="{python}", args=[str(SERVER)])


@pytest.fixture
async def provider():
    p = McpProvider(echo_spec())
    await p.start()
    try:
        yield p
    finally:
        await p.stop()


async def test_lists_tools_with_annotations(provider: McpProvider):
    specs = {s.name: s for s in await provider.list_tools()}
    assert set(specs) == {"echo.echo", "echo.write", "echo.wipe", "echo.boom"}
    assert specs["echo.echo"].read_only and specs["echo.echo"].idempotent
    assert not specs["echo.write"].read_only and not specs["echo.write"].destructive
    assert specs["echo.wipe"].destructive
    assert specs["echo.echo"].input_schema["properties"]["text"]["type"] == "string"
    assert provider.tool_count == 4 and provider.error is None


async def test_call_data_and_error(provider: McpProvider):
    ok = await provider.call("echo.echo", {"text": "hi"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=10)
    assert ok.kind is ToolResultKind.DATA and ok.text == "echo:hi"
    bad = await provider.call("echo.boom", {}, cancel=asyncio.Event(), idempotency_key="k2", timeout_s=10)
    assert bad.kind is ToolResultKind.ERROR and "kaboom" in bad.text


async def test_unreachable_server_is_an_error_not_a_crash():
    spec = McpServerSpec(name="dead", transport=McpTransport.STREAMABLE_HTTP, url="http://127.0.0.1:9/mcp", timeout_s=2)
    p = McpProvider(spec)
    await p.start()
    assert p.error is not None and not p.connected
    res = await p.call("dead.x", {}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=2)
    assert res.kind is ToolResultKind.ERROR and "unavailable" in res.text
    await p.stop()


async def test_mcp_tools_flow_through_the_loop(harness: Harness):
    s = harness.core.settings.model_copy(deep=True)
    s.mcp_servers = [echo_spec()]
    harness.core.apply_settings(s)
    await harness.core.reload_tools()
    assert "echo.echo" in {t.name for t in harness.core.registry.specs()}

    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="echo.echo", arguments={"text": "via mcp"})]),
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=20)
    tr = next(e for e in seen if e.type == "tool.result")
    assert tr.result.text == "echo:via mcp"
    call = next(e for e in seen if e.type == "tool.call")
    assert call.read_only is True
    health = harness.core.registry.provider_health()
    echo = next(h for h in health if h["name"] == "echo")
    assert echo["ok"] and echo["tools"] == 4
