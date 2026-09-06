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
    assert client.get("/api/health").status_code == 200  # open on purpose (down vs wrong token)
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers=_h()).status_code == 200
    assert client.get("/api/settings?token=secret").status_code == 200


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


def test_a_negative_limit_does_not_mean_unlimited(client: TestClient):
    """SQLite reads LIMIT -1 as "no limit", so a ceiling has to hold at BOTH ends.

    The endpoints clamped with `min(limit, N)` only, so ?limit=-1 asked for the whole table —
    every entity in the graph, every run of a conversation — from an endpoint whose contract
    says 50.
    """
    from jarvis_core.api.features import _clamp

    assert [_clamp(v, 200, default=50) for v in (-1, 0, 1, 50, 500)] == [50, 50, 1, 50, 200]

    # And the endpoints themselves answer rather than fall over.
    conv = client.post("/api/conversations", json={"title": "hits"}, headers=_h()).json()["id"]
    for limit in (-1, 0, 9999):
        for url in (
            f"/api/conversations/{conv}/runs?limit={limit}",
            f"/api/kg/entities?limit={limit}",
            f"/api/kg/graph?limit={limit}",
            f"/api/search?q=hits&limit={limit}",
            f"/api/schedules/none/fires?limit={limit}",
        ):
            assert client.get(url, headers=_h()).status_code == 200, url


def test_opening_a_conversation_tells_every_client_it_is_read(client: TestClient):
    """The unread badge has to clear on the phone as well as in the tab that opened the chat.

    The WS marked the conversation read in the database and published nothing, so any other
    connected client kept its badge until something unrelated republished the conversation.
    """
    conv = client.post("/api/conversations", json={"title": "unread one"}, headers=_h()).json()["id"]
    client.patch(f"/api/conversations/{conv}", json={"unread": True}, headers=_h())
    assert client.get(f"/api/conversations/{conv}", headers=_h()).json()["unread"] is True

    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_text(json.dumps({"type": "subscribe", "conversation_id": conv}))
        ws.send_text(json.dumps({"type": "ping"}))  # a fence, so a missing event fails fast
        seen: list[dict] = []
        while True:
            ev = json.loads(ws.receive_text())
            seen.append(ev)
            if ev["type"] == "pong":
                break
        updates = [e for e in seen if e["type"] == "conversation.updated" and e["conversation"]["id"] == conv]
        assert updates, f"marking the conversation read announced nothing: {[e['type'] for e in seen]}"
        assert updates[-1]["conversation"]["unread"] is False
    assert client.get(f"/api/conversations/{conv}", headers=_h()).json()["unread"] is False


def test_status_reports_endpoints(client: TestClient):
    fake_roles = fake_settings().model_dump(mode="json")["roles"]
    client.patch("/api/settings", json={"roles": fake_roles}, headers=_h())
    r = client.get("/api/status", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert len(body["endpoints"]) == 5 and all(e["ok"] for e in body["endpoints"])
    assert body["runs"] == {"running": 0, "queued": 0}
