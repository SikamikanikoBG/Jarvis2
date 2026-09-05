"""The browser extension leg: hello registers tools, calls round-trip, disconnect drops them."""

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


def test_browser_tools_register_roundtrip_and_unregister(client: TestClient):
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
    # Disconnected: tools gone, provider reports it honestly (the server's cleanup runs just
    # after the client socket closes, so poll briefly).
    import time

    for _ in range(50):
        if not any(t["name"].startswith("browser.") for t in client.get("/api/tools").json()):
            break
        time.sleep(0.05)
    assert not any(t["name"].startswith("browser.") for t in client.get("/api/tools").json())
    browser = next(p for p in client.get("/api/status").json()["tools"] if p["name"] == "browser")
    assert browser["ok"] is False and "not connected" in browser["error"]


def test_health_is_open_without_token(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    core.adapters.fakes = {r: FakeAdapter() for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/tools").status_code == 401
