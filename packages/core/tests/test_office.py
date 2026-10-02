"""The Office: runs become pixel-agents characters (features/office.py, api/office.py)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.features.office import IDLE_CLOSE_S, tool_status
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import RoleName, ToolCall
from jarvis_proto.events import ConversationDeleted
from tests.conftest import Harness
from tests.test_loop import with_tools


async def _drain(queue: asyncio.Queue[dict[str, Any]], until: str, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while True:
            seen.append(await queue.get())
            if seen[-1]["type"] == until and (until != "agentStatus" or seen[-1]["status"] == "waiting"):
                return seen


async def test_a_run_walks_in_works_and_finishes(harness: Harness):
    await with_tools(harness)
    office = harness.core.office
    await office.start()
    client = office.connect()
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.echo", arguments={"text": "hello"})]),
        FakeTurn(text="done"),
    )
    conv = await harness.core.store.create_conversation(title="Echo test")
    await harness.core.engine.create_run(text="echo", conversation_id=conv.id)

    seen = await _drain(client, "agentStatus")
    types = [m["type"] for m in seen]
    created = seen[0]
    assert created == {"type": "agentCreated", "id": created["id"], "folderName": "Echo test"}
    start = next(m for m in seen if m["type"] == "agentToolStart")
    # A read-only tool reads at the desk; the label names the tool and its argument.
    assert start["toolName"] == "Read" and start["toolId"] == "c1" and start["status"] == "test.echo · hello"
    assert types.index("agentToolStart") < types.index("agentToolDone") < types.index("agentToolsClear")
    assert seen[-1] == {"type": "agentStatus", "id": created["id"], "status": "waiting", "awaitingInput": False}
    assert office.view() == [
        {
            "id": created["id"],
            "conversation_id": conv.id,
            "title": "Echo test",
            "active": False,
            "permission": False,
            "awaiting_input": False,
            "tools": [],
        }
    ]


async def test_a_confirmation_raises_the_flag_and_lowers_it(harness: Harness):
    await with_tools(harness)
    office = harness.core.office
    await office.start()
    client = office.connect()
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="send", conversation_id=conv.id)
    await harness.wait_for(sub, "run.waiting_user")
    await asyncio.sleep(0.05)

    agent = office.view()[0]
    assert agent["permission"] is True and agent["awaiting_input"] is False
    harness.chat.push(FakeTurn(text="sent"))
    assert await harness.core.engine.confirm(run.id, "c1", True)
    seen = await _drain(client, "agentStatus")
    types = [m["type"] for m in seen]
    assert "agentToolPermission" in types and "agentToolPermissionClear" in types
    assert types.index("agentToolPermission") < types.index("agentToolPermissionClear")


async def test_an_idle_character_leaves_and_a_deleted_chat_takes_its_character(harness: Harness):
    office = harness.core.office
    client = office.connect()
    conv = await harness.core.store.create_conversation(title="Short one")
    other = await harness.core.store.create_conversation(title="Other")
    async with office.job("triage", "Mail triage"):
        assert office.view()[0]["tools"] == ["Mail triage"]
    harness.chat.push(FakeTurn(text="a"), FakeTurn(text="b"))
    await office.start()
    sub = harness.subscribe(conv.id)
    sub.conversations.add(other.id)
    await harness.core.engine.create_run(text="a", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    await harness.core.engine.create_run(text="b", conversation_id=other.id)
    await harness.wait_for(sub, "run.done")
    await asyncio.sleep(0.05)
    assert [a["title"] for a in office.view()] == ["Mail triage", "Short one", "Other"]

    await office.on_event(ConversationDeleted(conversation_id=other.id))
    assert [a["title"] for a in office.view()] == ["Mail triage", "Short one"]
    office.reap(time.monotonic() + IDLE_CLOSE_S + 1)
    assert office.view() == []
    messages: list[dict[str, Any]] = []
    while not client.empty():
        messages.append(client.get_nowait())
    assert sum(m["type"] == "agentClosed" for m in messages) == 3


def test_tool_status_is_short_and_one_line():
    assert tool_status("mail_search", {"limit": 5, "query": "  invoice\n march "}) == "mail_search · invoice march"
    assert tool_status("jarvis.time", {}) == "jarvis.time"
    long = tool_status("fs_read", {"path": "x" * 200})
    assert len(long) == 60 and long.endswith("…")


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    dist = tmp_path / "office-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>office</title>", encoding="utf-8")
    boot = {
        "assets": [{"type": "characterSpritesLoaded", "characters": []}, {"type": "floorTilesLoaded", "sprites": []}],
        "defaultLayout": {"version": 1, "cols": 2, "rows": 2},
    }
    (dist / "boot.json").write_text(json.dumps(boot), encoding="utf-8")
    core = Core(CoreConfig(home=tmp_path / "home", token="secret", office_dist=dist))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.core = core  # type: ignore[attr-defined]
        yield c


def _handshake(ws: Any) -> list[dict[str, Any]]:
    ws.send_text(json.dumps({"type": "webviewReady"}))
    seen: list[dict[str, Any]] = []
    while not seen or seen[-1]["type"] != "layoutLoaded":
        seen.append(json.loads(ws.receive_text()))
    return seen


def test_handshake_follows_the_pixel_agents_order_and_layout_is_kept(client: TestClient):
    assert client.get("/pixel-office/").status_code == 200
    assert client.get("/api/office/agents", headers={"Authorization": "Bearer secret"}).json() == {
        "built": True,
        "agents": [],
    }
    with client.websocket_connect("/pixel-office/ws?token=secret") as ws:
        seen = _handshake(ws)
        assert [m["type"] for m in seen] == [
            "providerCapabilities",
            "characterSpritesLoaded",
            "floorTilesLoaded",
            "settingsLoaded",
            "areaMappingsLoaded",
            "existingAgents",
            "layoutLoaded",
        ]
        assert seen[0]["readingTools"] == ["Read"]
        assert seen[-1]["layout"] == {"version": 1, "cols": 2, "rows": 2}
        ws.send_text(json.dumps({"type": "saveLayout", "layout": {"version": 1, "cols": 9, "rows": 9}}))
        ws.send_text(json.dumps({"type": "setSoundEnabled", "enabled": True}))
        seen = _handshake(ws)
        assert seen[-1]["layout"] == {"version": 1, "cols": 9, "rows": 9}
        assert next(m for m in seen if m["type"] == "settingsLoaded")["soundEnabled"] is True


def test_office_socket_and_agents_need_the_token(client: TestClient):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/pixel-office/ws?token=wrong") as ws:
        ws.receive_text()
    assert client.get("/api/office/agents").status_code == 401
