"""The browser extension leg: hello registers tools, calls round-trip, a disconnect keeps them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import RoleName, ToolCall


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=None))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.fake = fake  # type: ignore[attr-defined]
        c.core = core  # type: ignore[attr-defined]
        yield c


HELLO = {
    "type": "browser.hello",
    "agent": "jarvis-extension",
    "version": "2.0.0",
    "tools": [
        {
            "name": "browser.tabs",
            "description": "List open tabs",
            "input_schema": {"type": "object", "properties": {}},
            "read_only": True,
        },
        {
            "name": "browser.click",
            "description": "Click",
            "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
        },
    ],
}


def test_browser_tools_register_roundtrip_and_survive_a_disconnect(client: TestClient):
    with client.websocket_connect("/ws?client=browser") as ext:
        ext.send_text(json.dumps(HELLO))
        ready = json.loads(ext.receive_text())
        assert ready == {"type": "browser.ready", "tools": 2}
        tools = {t["name"]: t for t in client.get("/api/tools").json()}
        assert tools["browser.tabs"]["read_only"] is True and tools["browser.click"]["provider"] == "browser"
        status = client.get("/api/status").json()
        assert next(p for p in status["tools"] if p["name"] == "browser")["tools"] == 2

        # A chat run calls browser.tabs; the extension answers.
        client.fake.push(  # type: ignore[attr-defined]
            FakeTurn(tool_calls=[ToolCall(id="c1", name="browser.tabs", arguments={})]),
            FakeTurn(text="you have 2 tabs"),
        )
        with client.websocket_connect("/ws") as ui:
            ui.send_text(json.dumps({"type": "run.create", "text": "what tabs are open?"}))
            call = json.loads(ext.receive_text())
            assert call["type"] == "browser.call" and call["name"] == "browser.tabs"
            ext.send_text(
                json.dumps(
                    {"type": "browser.result", "call_id": call["call_id"], "kind": "data", "text": "1. GitHub\n2. Mail"}
                )
            )
            while True:
                ev = json.loads(ui.receive_text())
                if ev["type"] == "tool.result":
                    assert ev["result"]["text"] == "1. GitHub\n2. Mail"
                if ev["type"] in {"run.done", "run.failed"}:
                    assert ev["type"] == "run.done"
                    break
        ext.send_text(json.dumps({"type": "browser.context", "url": "https://x.y/z", "title": "Zed"}))
        ext.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ext.receive_text())["type"] == "pong"
        assert client.core.browser.context["title"] == "Zed"  # type: ignore[attr-defined]
    # Disconnected: the provider says so honestly, and the TOOLS STAY.
    #
    # They used to be dropped, which reads tidy and cost a fortune: the chat template renders the
    # tool list into the system message, so those two tools leaving re-prefilled every cached
    # conversation — measured at 17 s a turn, every time Arsen closed his browser. Closing a
    # browser is not losing a capability; a call made meanwhile answers "not connected".
    import time

    browser = None
    for _ in range(50):
        browser = next(p for p in client.get("/api/status").json()["tools"] if p["name"] == "browser")
        if not browser["ok"]:
            break
        time.sleep(0.05)
    assert browser is not None and browser["ok"] is False and "not connected" in browser["error"]
    names = [t["name"] for t in client.get("/api/tools").json()]
    assert "browser.tabs" in names and "browser.click" in names
    # ...and calling one now fails with that reason instead of "unknown tool".
    import asyncio

    result = client.portal.call(
        lambda: client.core.registry.call(  # type: ignore[attr-defined]
            "browser.tabs", {}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5
        )
    )
    assert result.kind.value == "error" and "not connected" in result.text


def test_health_is_open_without_token(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    core.adapters.fakes = {r: FakeAdapter() for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/tools").status_code == 401
