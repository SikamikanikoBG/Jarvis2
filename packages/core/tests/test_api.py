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
    assert len(body["endpoints"]) == len(RoleName) and all(e["ok"] for e in body["endpoints"])
    assert body["runs"] == {"running": 0, "queued": 0}


def test_settings_stored_before_the_background_role_still_load():
    """A roles map saved by an older core has no 'background' row; it comes back as a copy of
    chat (same endpoint), never as the built-in localhost default. run_routing likewise gets
    its defaults."""
    from jarvis_proto import ModelSpec, Provider, RoleName, RunKind, Settings

    old = Settings().model_dump(mode="json")
    old["roles"].pop("background")
    old.pop("run_routing")
    old["roles"]["chat"]["base_url"] = "http://vader:18021/v1"
    old["roles"]["chat"]["provider"] = Provider.VLLM.value
    loaded = Settings.model_validate(old)
    assert loaded.roles[RoleName.BACKGROUND] == ModelSpec.model_validate(old["roles"]["chat"])
    assert loaded.lane_for(RunKind.SCHEDULED) is RoleName.CHAT
    assert loaded.lane_for(RunKind.TRIAGE) is RoleName.BACKGROUND
    assert loaded.lane_for(RunKind.CHAT) is RoleName.CHAT


def test_run_routing_only_accepts_lanes(client: TestClient):
    r = client.patch("/api/settings", json={"run_routing": {"scheduled": "planner"}}, headers=_h())
    assert r.status_code == 422
    r = client.patch("/api/settings", json={"run_routing": {"scheduled": "chat"}}, headers=_h())
    assert r.status_code == 200 and r.json()["run_routing"]["scheduled"] == "chat"


def test_every_call_of_a_run_goes_to_its_lane():
    """Inside a run the planner keeps its behaviour (no thinking, low temperature) but takes the
    lane's endpoint; outside a run it uses its own."""
    from jarvis_core.models.factory import AdapterFactory, current_run_kind
    from jarvis_proto import ModelSpec, Provider, RoleName, RunKind, Settings

    s = Settings()
    s.roles[RoleName.CHAT] = ModelSpec(provider=Provider.VLLM, base_url="http://chat-lane/v1", model="m", think=True)
    s.roles[RoleName.BACKGROUND] = ModelSpec(provider=Provider.VLLM, base_url="http://bg-lane/v1", model="m", think=True)
    s.roles[RoleName.PLANNER] = ModelSpec(provider=Provider.OLLAMA, base_url="http://elsewhere", model="p", temperature=0.1)
    f = AdapterFactory(s)
    assert f.spec_for(RoleName.PLANNER).base_url == "http://elsewhere"
    planner_in_scheduled = f.spec_for(RoleName.PLANNER, kind=RunKind.TRIAGE)
    assert planner_in_scheduled.base_url == "http://bg-lane/v1" and planner_in_scheduled.provider is Provider.VLLM
    assert planner_in_scheduled.temperature == 0.1 and planner_in_scheduled.think is False
    assert f.spec_for(RoleName.CHAT, kind=RunKind.TRIAGE).base_url == "http://bg-lane/v1"
    token = current_run_kind.set(RunKind.CHAT)
    try:
        assert f.spec_for(RoleName.PLANNER).base_url == "http://chat-lane/v1"
        assert f.for_role(RoleName.CHAT).spec.base_url == "http://chat-lane/v1"
    finally:
        current_run_kind.reset(token)


def test_effective_budgets_follow_the_lane_window():
    from jarvis_proto import Settings

    s = Settings(history_token_budget=24_000, tool_context_token_budget=40_000, context_reserve_tokens=2_048)
    # No window known, or a window with room: as configured.
    assert s.effective_budgets(window=None, max_tokens=16_384, fixed_tokens=16_000) == (24_000, 40_000)
    assert s.effective_budgets(window=262_144, max_tokens=16_384, fixed_tokens=16_000) == (24_000, 40_000)
    # A 64k lane: 65,536 - 16,384 - 16,000 - 2,048 = 31,104 to share 3:5.
    h, r = s.effective_budgets(window=65_536, max_tokens=16_384, fixed_tokens=16_000)
    assert (h, r) == (11_664, 19_440) and h + r == 31_104
    # Nothing left: the floor, not zero — the server's refusal is the honest failure then.
    h, r = s.effective_budgets(window=20_000, max_tokens=16_384, fixed_tokens=16_000)
    assert h + r == 4_000
    with pytest.raises(ValueError):
        Settings(tool_result_admit_chars=500)
    # One result never takes more than half the step's results budget on a small lane.
    assert s.admit_chars(40_000, 3.2) == 48_000
    assert s.admit_chars(10_000, 3.2) == 16_000
