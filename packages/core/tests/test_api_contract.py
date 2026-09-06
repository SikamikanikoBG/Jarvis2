"""Every REST route the web UI calls, walked end to end through the real ASGI app.

The UI is the only client of most of these and it is not in the test suite, so a route that
answers 500 — or 200 with the wrong shape — reaches Arsen's phone before anything notices.
This walks each resource through its whole life (create, read, change, list, delete) and
asserts the *contract*: the status code, the identity of what comes back, and what a missing
id does. It is deliberately shallow per route and complete across them.
"""

from __future__ import annotations

import io
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter
from jarvis_proto import RoleName

TOKEN = "secret"


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=TOKEN))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        c.core = core  # type: ignore[attr-defined]
        yield c


def h() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def ok(response, *expected: int):  # type: ignore[no-untyped-def]
    assert response.status_code in expected, f"{response.request.method} {response.request.url}: {response.text[:300]}"
    return response


# --- boards and notes -----------------------------------------------------------------------


def test_boards_and_notes_full_lifecycle(client: TestClient):
    board = ok(client.post("/api/boards", json={"name": " Work "}, headers=h()), 201).json()
    assert board["name"] == "Work" and board["note_count"] == 0
    second = ok(client.post("/api/boards", json={"name": "Home"}, headers=h()), 201).json()
    assert [b["id"] for b in ok(client.get("/api/boards", headers=h()), 200).json()] == [board["id"], second["id"]]
    assert ok(client.post("/api/boards", json={"name": "  "}, headers=h()), 422)

    renamed = ok(client.patch(f"/api/boards/{board['id']}", json={"name": "Work stuff"}, headers=h()), 200).json()
    assert renamed["name"] == "Work stuff"
    # Reordering renumbers everything; a patch with only a position must not 404.
    moved = ok(client.patch(f"/api/boards/{board['id']}", json={"position": 1}, headers=h()), 200).json()
    assert moved["position"] == 1
    assert [b["name"] for b in client.get("/api/boards", headers=h()).json()] == ["Home", "Work stuff"]
    ok(client.patch("/api/boards/brd_missing", json={"name": "x"}, headers=h()), 404)

    note = ok(
        client.post(f"/api/boards/{board['id']}/notes", json={"text": "Ring Rumen", "color": "blue"}, headers=h()), 201
    ).json()
    assert note["text"] == "Ring Rumen" and note["color"] == "blue"
    ok(client.post(f"/api/boards/{board['id']}/notes", json={"text": " "}, headers=h()), 422)
    ok(client.post("/api/boards/brd_missing/notes", json={"text": "x"}, headers=h()), 404)
    assert len(ok(client.get(f"/api/boards/{board['id']}/notes", headers=h()), 200).json()) == 1
    ok(client.get("/api/boards/brd_missing/notes", headers=h()), 404)

    edited = ok(client.patch(f"/api/notes/{note['id']}", json={"text": "Ring Rumen at 5"}, headers=h()), 200).json()
    assert edited["text"] == "Ring Rumen at 5" and edited["color"] == "blue"  # untouched fields survive
    ok(client.patch("/api/notes/note_missing", json={"text": "x"}, headers=h()), 404)
    # Moving a note to the other board.
    moved_note = ok(
        client.patch(f"/api/notes/{note['id']}", json={"board_id": second["id"]}, headers=h()), 200
    ).json()
    assert moved_note["board_id"] == second["id"]
    assert client.get(f"/api/boards/{board['id']}/notes", headers=h()).json() == []

    ok(client.delete(f"/api/notes/{note['id']}", headers=h()), 204)
    ok(client.delete(f"/api/boards/{board['id']}", headers=h()), 204)
    assert [b["id"] for b in client.get("/api/boards", headers=h()).json()] == [second["id"]]


# --- knowledge ------------------------------------------------------------------------------


def test_knowledge_entities_graph_merge(client: TestClient):
    core = client.core  # type: ignore[attr-defined]
    call = client.portal.call  # runs a coroutine on the app's own loop
    assert client.get("/api/kg/entities", headers=h()).json() == []

    rumen = call(partial(core.knowledge.upsert, "Rumen Petrov", type="person", summary="Head of retail"))
    phoenix = call(partial(core.knowledge.upsert, "Project Phoenix", type="project"))
    duplicate = call(partial(core.knowledge.upsert, "R. Petrov", type="person"))
    call(partial(core.knowledge.add_edge, rumen.id, phoenix.id, "sponsors"))

    found = ok(client.get("/api/kg/entities?q=Rumen", headers=h()), 200).json()
    assert [e["name"] for e in found] == ["Rumen Petrov"]
    assert len(ok(client.get("/api/kg/entities", headers=h()), 200).json()) == 3

    detail = ok(client.get(f"/api/kg/entities/{rumen.id}", headers=h()), 200).json()
    assert detail["summary"] == "Head of retail"
    assert [(e["relation"], e["other"]["name"]) for e in detail["edges"]] == [("sponsors", "Project Phoenix")]

    graph = ok(client.get(f"/api/kg/graph?center={rumen.id}&depth=1&limit=10", headers=h()), 200).json()
    assert rumen.id in {n["id"] for n in graph["nodes"]} and len(graph["edges"]) == 1

    patched = ok(
        client.patch(f"/api/kg/entities/{rumen.id}", json={"summary": "Head of retail banking"}, headers=h()), 200
    ).json()
    assert patched["summary"] == "Head of retail banking" and patched["name"] == "Rumen Petrov"

    merged = ok(
        client.post(f"/api/kg/entities/{duplicate.id}/merge", json={"into": rumen.id}, headers=h()), 200
    ).json()
    assert merged["id"] == rumen.id and "R. Petrov" in merged["aliases"]
    assert len(client.get("/api/kg/entities", headers=h()).json()) == 2

    ok(client.delete(f"/api/kg/entities/{phoenix.id}", headers=h()), 204)
    assert len(client.get("/api/kg/entities", headers=h()).json()) == 1
    ok(client.get("/api/kg/entities/ent_missing", headers=h()), 404)
    ok(client.patch("/api/kg/entities/ent_missing", json={"summary": "x"}, headers=h()), 404)
    ok(client.post("/api/kg/entities/ent_missing/merge", json={"into": rumen.id}, headers=h()), 404)
    ok(client.delete("/api/kg/entities/ent_missing", headers=h()), 204)  # delete is idempotent


# --- skills ---------------------------------------------------------------------------------


SKILL = "---\ndescription: How to write the weekly status\ntriggers: [weekly status]\n---\nThree sections."


def test_skills_crud_and_input_validation(client: TestClient):
    assert client.get("/api/skills", headers=h()).json() == []
    created = ok(client.put("/api/skills/weekly_status", json={"content": SKILL}, headers=h()), 200).json()
    assert created["name"] == "weekly_status" and created["enabled"] is True and created["size"] > 0
    assert created["triggers"] == ["weekly status"]
    body = ok(client.get("/api/skills/weekly_status", headers=h()), 200).json()
    assert body["content"] == SKILL

    # Frontmatter without a description is refused, and so is a name that is not a safe filename.
    ok(client.put("/api/skills/x", json={"content": "no frontmatter"}, headers=h()), 422)
    for bad in ("Weekly Status", "a" * 80, "..escape"):
        ok(client.put(f"/api/skills/{bad}", json={"content": SKILL}, headers=h()), 422)
        ok(client.get(f"/api/skills/{bad}", headers=h()), 422)
    # The name becomes a filename, so the guard is asserted where it lives too: a traversal in
    # the URL never reaches the route (the router normalises it away) and must still be refused
    # by anything that calls the store directly.
    with pytest.raises(ValueError):
        client.core.skills._path("../escape")  # type: ignore[attr-defined]
    assert sorted(p.name for p in (client.core.config.home / "skills").iterdir()) == ["weekly_status.md"]  # type: ignore[attr-defined]

    off = ok(client.patch("/api/skills/weekly_status", json={"enabled": False}, headers=h()), 200).json()
    assert off["enabled"] is False
    ok(client.patch("/api/skills/nope", json={"enabled": False}, headers=h()), 404)
    ok(client.delete("/api/skills/weekly_status", headers=h()), 204)
    ok(client.delete("/api/skills/weekly_status", headers=h()), 404)
    assert client.get("/api/skills", headers=h()).json() == []


# --- schedules ------------------------------------------------------------------------------


def test_schedules_crud_validation_and_history(client: TestClient):
    ok(client.post("/api/schedules", json={"name": "x"}, headers=h()), 422)  # prompt missing
    ok(client.post("/api/schedules", json={"name": "x", "prompt": "y"}, headers=h()), 422)  # neither cron nor at
    ok(
        client.post("/api/schedules", json={"name": "x", "prompt": "y", "cron": "not a cron"}, headers=h()),
        422,
    )
    ok(
        client.post(
            "/api/schedules",
            json={"name": "x", "prompt": "y", "cron": "0 9 * * 1", "at": "2026-09-09T09:00:00"},
            headers=h(),
        ),
        422,
    )  # both

    made = ok(
        client.post("/api/schedules", json={"name": "Weekly", "prompt": "write it", "cron": "0 9 * * 1"}, headers=h()),
        201,
    ).json()
    assert made["enabled"] is True and made["next_fire"] and made["cron"] == "0 9 * * 1"
    assert ok(client.get(f"/api/schedules/{made['id']}", headers=h()), 200).json()["id"] == made["id"]
    ok(client.get("/api/schedules/sch_missing", headers=h()), 404)

    paused = ok(client.patch(f"/api/schedules/{made['id']}", json={"enabled": False}, headers=h()), 200).json()
    assert paused["enabled"] is False and paused["next_fire"] is None
    resumed = ok(client.patch(f"/api/schedules/{made['id']}", json={"enabled": True}, headers=h()), 200).json()
    assert resumed["enabled"] is True and resumed["next_fire"]
    # Switching to a one-shot clears the cron rather than keeping both.
    one_shot = ok(
        client.patch(f"/api/schedules/{made['id']}", json={"at": "2099-01-01T09:00:00"}, headers=h()), 200
    ).json()
    assert one_shot["cron"] is None and one_shot["at"]
    ok(client.patch(f"/api/schedules/{made['id']}", json={"cron": "bad"}, headers=h()), 422)
    ok(client.patch("/api/schedules/sch_missing", json={"name": "x"}, headers=h()), 404)

    assert client.get(f"/api/schedules/{made['id']}/fires", headers=h()).json() == []
    fired = ok(client.post(f"/api/schedules/{made['id']}/run", headers=h()), 200).json()
    assert fired["run_id"] and fired["conversation_id"]
    fires = ok(client.get(f"/api/schedules/{made['id']}/fires", headers=h()), 200).json()
    assert [f["run_id"] for f in fires] == [fired["run_id"]]
    ok(client.post("/api/schedules/sch_missing/run", headers=h()), 404)
    ok(client.delete(f"/api/schedules/{made['id']}", headers=h()), 204)
    assert client.get("/api/schedules", headers=h()).json() == []


# --- attachments ----------------------------------------------------------------------------


def test_attachment_routes(client: TestClient):
    conv = ok(client.post("/api/conversations", json={"title": "with files"}, headers=h()), 201).json()["id"]
    text_att = ok(
        client.post("/api/attachments/text", json={"text": "a stack trace", "name": "paste.txt"}, headers=h()), 201
    ).json()
    assert text_att["kind"] == "text" and text_att["text"] == "a stack trace"
    ok(client.post("/api/attachments/text", json={"text": "   "}, headers=h()), 422)

    doc = ok(
        client.post(
            "/api/attachments",
            files={"file": ("notes.md", io.BytesIO(b"# Notes\nline"), "text/markdown")},
            data={"conversation_id": conv},
            headers=h(),
        ),
        201,
    ).json()
    assert doc["kind"] == "document"
    ok(
        client.post("/api/attachments", files={"file": ("x.bin", io.BytesIO(b"\x00\x01"), "application/octet-stream")}, headers=h()),
        422,
    )

    body = ok(client.get(f"/api/attachments/{doc['id']}", headers=h()), 200)
    assert b"# Notes" in body.content
    meta = ok(client.get(f"/api/attachments/{doc['id']}/text", headers=h()), 200).json()
    assert meta["name"] == "notes.md" and "line" in meta["text"]
    assert [a["id"] for a in ok(client.get(f"/api/conversations/{conv}/attachments", headers=h()), 200).json()] == [
        doc["id"]
    ]
    ok(client.get("/api/attachments/att_missing", headers=h()), 404)
    ok(client.get("/api/attachments/att_missing/text", headers=h()), 404)
    ok(client.delete(f"/api/attachments/{doc['id']}", headers=h()), 204)
    ok(client.delete(f"/api/attachments/{doc['id']}", headers=h()), 404)


# --- meetings, triage, rsvp, summary ----------------------------------------------------------


def test_background_job_routes_answer_without_a_host(client: TestClient):
    """None of these has a host configured here; each must SAY so rather than crash."""
    assert client.get("/api/meetings", headers=h()).json() == []
    ok(client.get("/api/meetings/mtg_missing", headers=h()), 404)
    ok(client.post("/api/meetings/mtg_missing/stop", headers=h()), 404)
    ok(client.delete("/api/meetings/mtg_missing", headers=h()), 404)
    ok(client.post("/api/meetings", json={"title": "x", "host": "nowhere"}, headers=h()), 502)

    assert client.get("/api/triage/state", headers=h()).json() == []
    report = ok(client.post("/api/triage/run?dry_run=true", headers=h()), 200).json()
    assert report["dry_run"] is True and any("triage.host" in e for e in report["errors"])
    ok(client.post("/api/triage/run?folder=Inbox", headers=h()), 422)  # a folder sample must be a dry run

    rsvp = ok(client.get("/api/rsvp/state", headers=h()), 200).json()
    assert rsvp["state"] is None and rsvp["recent"] == []
    run = ok(client.post("/api/rsvp/run?dry_run=true", headers=h()), 200).json()
    assert any("rsvp.host" in e for e in run["errors"])

    conv = ok(client.post("/api/conversations", json={"title": "no summary yet"}, headers=h()), 201).json()["id"]
    assert ok(client.get(f"/api/conversations/{conv}/summary", headers=h()), 200).json() is None


def test_search_and_fork(client: TestClient):
    conv = ok(client.post("/api/conversations", json={"title": "Budget approval"}, headers=h()), 201).json()["id"]
    hits = ok(client.get("/api/search?q=Budget", headers=h()), 200).json()
    assert [x["conversation"]["id"] for x in hits] == [conv] and hits[0]["matched"] == "title"
    assert client.get("/api/search?q=", headers=h()).json() == []
    # LIKE metacharacters in the query are literal, not wildcards.
    assert client.get("/api/search?q=%25", headers=h()).json() == []

    fork = ok(client.post(f"/api/conversations/{conv}/fork", json={}, headers=h()), 201).json()
    assert fork["id"] != conv and fork["title"].endswith("(fork)")
    ok(client.post(f"/api/conversations/{conv}/fork", json={"up_to_message_id": "msg_nope"}, headers=h()), 422)
    ok(client.post("/api/conversations/conv_missing/fork", json={}, headers=h()), 404)


def _get_paths(routes: object) -> list[str]:
    """Every GET path in the app.

    This FastAPI keeps an included router as an opaque `_IncludedRouter` wrapper in app.routes
    instead of flattening its routes in, so the walk follows `original_router` as well as the
    usual `routes` attribute. Missing that is how a whole router would slip past this test.
    """
    out: list[str] = []
    for route in getattr(routes, "routes", routes) or []:  # type: ignore[union-attr]
        inner = getattr(route, "original_router", None) or getattr(route, "routes", None)
        if inner is not None:
            out.extend(_get_paths(inner))
            continue
        path = getattr(route, "path", "")
        if path and "GET" in (getattr(route, "methods", set()) or set()):
            out.append(path)
    return out


def test_every_api_route_refuses_a_missing_token(client: TestClient):
    """One dependency guards them all — including the OpenAPI schema and Swagger UI.

    FastAPI's built-in docs_url/openapi_url take no dependencies, so /api/openapi.json served
    the full API description — every route, parameter and model — to anyone who could reach the
    port. This core is on Tailscale and its /collab is tunnelled publicly.
    """
    # /api/health is open on purpose: the UI uses it to tell "down" from "wrong token".
    open_on_purpose = {"/api/health"}
    substitutions = {
        "conversation_id": "c", "run_id": "r", "schedule_id": "s", "entity_id": "e", "name": "n",
        "attachment_id": "a", "meeting_id": "m", "board_id": "b", "note_id": "no", "seq": "1",
    }
    checked: list[str] = []
    for path in _get_paths(client.app):
        if not path.startswith("/api/") or path in open_on_purpose:
            continue
        probe = path
        for key, value in substitutions.items():
            probe = probe.replace(f"{{{key}}}", value)
        assert "{" not in probe, f"unmapped path parameter in {path}"
        assert client.get(probe).status_code == 401, f"{probe} answered without a token"
        checked.append(probe)
    assert "/api/openapi.json" in checked and "/api/docs" in checked
    assert len(checked) >= 15, f"only {len(checked)} GET routes were probed: {checked}"
    # ...and the owner still gets them.
    assert ok(client.get("/api/openapi.json", headers=h()), 200).json()["info"]["title"] == "Jarvis V2"
    assert ok(client.get("/api/docs", headers=h()), 200).text.count("swagger") > 0
