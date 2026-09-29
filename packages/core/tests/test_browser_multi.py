"""Several browsers at once (2026-09-29: the laptop's Brave was refused for as long as the VM's
held the one slot). Each is named; a call goes where it is told, else where the chat already
browses, else to the browser Arsen used last."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.current import current_conversation_id
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter
from jarvis_core.tools.ws_provider import WsProvider
from jarvis_proto import McpServerSpec, RoleName

TOOLS = [
    {"name": "browser.tabs", "description": "List open tabs", "input_schema": {"type": "object", "properties": {}}},
    {
        "name": "browser.open",
        "description": "Open a URL",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
]


def hello(name: str | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "browser.hello", "agent": "jarvis-extension", "version": "2.4.0", "tools": TOOLS}
    if name:
        frame["name"] = name
    return frame


class Ext:
    """A fake extension: records what the core sends and answers every call with its name."""

    def __init__(self, provider: WsProvider, name: str) -> None:
        self.provider = provider
        self.sent: list[dict[str, Any]] = []
        self.conn = provider.connect(self.send, hello(name))

    async def send(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)
        if frame["type"] == "browser.call":
            asyncio.get_running_loop().call_soon(
                self.provider.handle_result,
                {"call_id": frame["call_id"], "kind": "data", "text": f"from {self.conn.name}"},
            )

    def calls(self) -> list[dict[str, Any]]:
        return [f for f in self.sent if f["type"] == "browser.call"]


async def call(provider: WsProvider, session: str, args: dict[str, Any] | None = None) -> str:
    token = current_conversation_id.set(session)
    try:
        result = await provider.call(
            "browser.tabs", args or {}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5
        )
    finally:
        current_conversation_id.reset(token)
    return result.text


async def test_two_browsers_route_by_name_by_chat_and_by_who_arsen_used_last():
    provider = WsProvider()
    laptop = Ext(provider, "workocholic")
    vm = Ext(provider, "jarvisvm")  # connected last, so it is the default for now
    assert provider.names == ["jarvisvm", "workocholic"]

    # Named: goes there, and the extension never sees the routing argument.
    assert await call(provider, "chat-a", {"browser": "workocholic"}) == "from workocholic"
    assert "browser" not in laptop.calls()[-1]["arguments"]
    # The same chat without a name stays where its work tab is.
    assert await call(provider, "chat-a") == "from workocholic"
    # A new chat goes to the browser used last...
    assert await call(provider, "chat-b") == "from jarvisvm"
    # ...which changes when Arsen opens the side panel in the other one.
    provider.handle_context({"url": "https://example.org", "title": "Example"}, laptop.conn)
    assert await call(provider, "chat-c") == "from workocholic"
    assert provider.context == {"url": "https://example.org", "title": "Example"}
    # A name that is not connected says who is.
    text = await call(provider, "chat-d", {"browser": "nope"})
    assert "no browser named 'nope'" in text and "jarvisvm, workocholic" in text

    # job_done reaches only the browsers that chat used.
    await provider.job_done("chat-a")
    assert {"type": "browser.job_done", "session": "chat-a"} in laptop.sent
    assert not any(f["type"] == "browser.job_done" for f in vm.sent)

    block = provider.context_block()
    assert block is not None and "Connected browsers: jarvisvm, workocholic" in block
    assert "Arsen is looking at (in workocholic)" in block


async def test_one_browser_leaving_keeps_the_other_and_a_chat_moves_on():
    provider = WsProvider()
    laptop = Ext(provider, "workocholic")
    vm = Ext(provider, "jarvisvm")
    assert await call(provider, "chat", {"browser": "workocholic"}) == "from workocholic"
    provider.disconnect(laptop.conn)
    assert provider.connected and provider.names == ["jarvisvm"]
    # Its work tab went with the laptop: the chat carries on in the browser that is left.
    assert await call(provider, "chat") == "from jarvisvm"
    provider.disconnect(vm.conn)
    assert not provider.connected
    assert "not connected" in await call(provider, "chat")
    # The tools stay listed (the prompt must not change when a browser closes), each with the
    # routing argument whose text never names who is connected.
    names = {t.name: t for t in await provider.list_tools()}
    assert set(names) == {"browser.tabs", "browser.open"}
    assert "browser" in names["browser.open"].input_schema["properties"]
    assert names["browser.open"].input_schema["required"] == ["url"]


async def test_a_reconnect_replaces_its_own_ghost_and_the_ghost_closing_late_changes_nothing():
    provider = WsProvider()
    first = Ext(provider, "workocholic")
    second = Ext(provider, "workocholic")
    assert provider.names == ["workocholic"]
    provider.disconnect(first.conn)  # the old socket's finally runs after the new hello
    assert provider.names == ["workocholic"]
    assert await call(provider, "chat") == "from workocholic"
    assert second.calls() and not first.calls()


def test_an_unnamed_extension_is_named_after_the_mcp_host_at_its_address(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=None))
    core.settings = core.settings.model_copy(
        update={
            "mcp_servers": [
                McpServerSpec(name="workocholic", transport="streamable_http", url="http://100.90.14.25:9030/mcp")
            ]
        }
    )
    assert core.browser.resolve_name({}, "100.90.14.25") == "workocholic"
    assert core.browser.resolve_name({}, "100.70.168.111") == "100.70.168.111"
    assert core.browser.resolve_name({"name": "brave-home"}, "100.90.14.25") == "brave-home"


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=None))
    core.adapters.fakes = {r: FakeAdapter() for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.core = core  # type: ignore[attr-defined]
        yield c


def test_a_second_extension_is_welcome_over_the_websocket(client: TestClient):
    with client.websocket_connect("/ws?client=browser") as a, client.websocket_connect("/ws?client=browser") as b:
        a.send_text(json.dumps(hello("workocholic")))
        assert json.loads(a.receive_text()) == {"type": "browser.ready", "tools": 2, "name": "workocholic"}
        b.send_text(json.dumps(hello("jarvisvm")))
        assert json.loads(b.receive_text()) == {"type": "browser.ready", "tools": 2, "name": "jarvisvm"}
        assert client.core.browser.names == ["jarvisvm", "workocholic"]  # type: ignore[attr-defined]
        status = next(p for p in client.get("/api/status").json()["tools"] if p["name"] == "browser")
        assert status["ok"] and status["tools"] == 2
    # b closed first, then a: nobody is left.
    import time

    for _ in range(50):
        if not client.core.browser.connected:  # type: ignore[attr-defined]
            break
        time.sleep(0.05)
    assert not client.core.browser.connected  # type: ignore[attr-defined]
