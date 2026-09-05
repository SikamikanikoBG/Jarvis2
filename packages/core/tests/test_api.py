"""REST + WS through the real ASGI app with the fake model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import RoleName
from tests.conftest import fake_settings


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        c.fake = fake  # type: ignore[attr-defined]
        c.core = core  # type: ignore[attr-defined]
        yield c


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_token_is_required(client: TestClient):
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers=_h()).status_code == 200
    assert client.get("/api/health?token=secret").status_code == 200


def test_conversation_crud_and_settings(client: TestClient):
    r = client.post("/api/conversations", json={"title": "t"}, headers=_h())
    assert r.status_code == 201
    cid = r.json()["id"]
    assert client.get("/api/conversations", headers=_h()).json()[0]["id"] == cid
    r = client.patch(f"/api/conversations/{cid}", json={"title": "renamed", "archived": True}, headers=_h())
    assert r.json()["title"] == "renamed" and r.json()["archived"] is True
    assert client.get("/api/conversations", headers=_h()).json() == []
    assert client.get("/api/conversations?archived=1", headers=_h()).json()[0]["id"] == cid
    assert client.delete(f"/api/conversations/{cid}", headers=_h()).status_code == 204
    assert client.get(f"/api/conversations/{cid}", headers=_h()).status_code == 404

    s = client.get("/api/settings", headers=_h()).json()
    assert s["assistant_name"] == "Jarvis"
    r = client.patch("/api/settings", json={"user_name": "A"}, headers=_h())
    assert r.status_code == 200 and r.json()["user_name"] == "A"
    bad = dict(s["roles"])
    bad["judge"] = {**bad["judge"], "think": True}
    r = client.patch("/api/settings", json={"roles": bad}, headers=_h())
    assert r.status_code == 422 and "may not think" in json.dumps(r.json())
    tools = client.get("/api/tools", headers=_h()).json()
    assert any(t["name"] == "jarvis.time" for t in tools)


def test_ws_chat_roundtrip(client: TestClient):
    client.fake.push(FakeTurn(text="hello from fake", reasoning="hmm"))  # type: ignore[attr-defined]
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"
        ws.send_text(json.dumps({"type": "run.create", "text": "hi there"}))
        seen: list[dict] = []
        while True:
            ev = json.loads(ws.receive_text())
            seen.append(ev)
            if ev["type"] in {"run.done", "run.failed", "run.cancelled"}:
                assert ev["type"] == "run.done", ev
                break
        types = [e["type"] for e in seen]
        assert types[0] == "conversation.updated"
        assert "run.queued" in types and "message.created" in types and "model.delta" in types
        run_id = next(e["run_id"] for e in seen if e["type"] == "run.queued")
        conv_id = seen[0]["conversation"]["id"]
        final = [e for e in seen if e["type"] == "message.created" and e["message"]["role"] == "assistant"]
        assert final and final[0]["message"]["content"] == "hello from fake"
        # Persisted events are queryable and contain no deltas.
        events = client.get(f"/api/runs/{run_id}/events", headers=_h()).json()
        assert events and all(e["type"] != "model.delta" for e in events)
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_h()).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        runs = client.get(f"/api/conversations/{conv_id}/runs", headers=_h()).json()
        assert runs[0]["status"] == "done"


def test_ws_rejects_bad_token(client: TestClient):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=wrong") as ws:
        ws.receive_text()


def test_status_reports_endpoints(client: TestClient):
    fake_roles = fake_settings().model_dump(mode="json")["roles"]
    client.patch("/api/settings", json={"roles": fake_roles}, headers=_h())
    r = client.get("/api/status", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert len(body["endpoints"]) == 5 and all(e["ok"] for e in body["endpoints"])
    assert body["runs"] == {"running": 0, "queued": 0}
