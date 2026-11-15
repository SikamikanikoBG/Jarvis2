"""Every /api route says whether Jarvis may call it (api.deps.agent, docs/stories/14_agent_admin.md).

jarvis.api_* reaches the UI's own routes, and "the UI's routes" is not a safe line by itself: some
of them mint credentials, approve Jarvis's own waiting calls or stream media. So a route opts in,
and this test makes a new route without a decision fail in the same diff - nobody keeps a list.
"""

from __future__ import annotations

from pathlib import Path

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.features.admin_api import MARKER, api_routes

# Never reachable whatever is marked: what a run must not do for itself (credentials, approvals,
# a push channel to an unknown browser, the media streams, the OpenAI-compatible model surface).
NEVER = {
    ("POST", "/api/collab/keys"),
    ("DELETE", "/api/collab/keys/{key_id}"),
    ("POST", "/api/tasks/{task_id}/allow-tool"),
    ("POST", "/api/push/devices"),
    ("POST", "/api/stt"),
    ("POST", "/api/tts"),
    ("POST", "/api/attachments"),
    ("POST", "/api/settings/changes/{change_id}/apply"),
    ("POST", "/api/schedules/{schedule_id}/accept"),
}


def test_every_api_route_has_an_agent_decision(tmp_path: Path):
    app = create_app(CoreConfig(home=tmp_path, token=None), core=Core(CoreConfig(home=tmp_path, token=None)))
    undecided, reachable = [], set()
    for route in api_routes(app.routes):
        if not route.path.startswith("/api/") or not route.include_in_schema:
            continue
        marker = (route.openapi_extra or {}).get(MARKER)
        for method in route.methods or set():
            if marker is None:
                undecided.append(f"{method} {route.path}")
            elif marker["access"] in ("read", "write") and not marker.get("prefer"):
                reachable.add((method, route.path))
    assert not undecided, "mark these with openapi_extra=agent(...): " + ", ".join(sorted(undecided))
    assert not NEVER & reachable, NEVER & reachable
    # The UI's everyday edits are there.
    assert {("PATCH", "/api/boards/{board_id}"), ("POST", "/api/folders"), ("PUT", "/api/skills/{name}")} <= reachable
    assert not any(path.startswith("/v1/") for _m, path in reachable)
