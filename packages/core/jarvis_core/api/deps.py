"""Request-scoped access to the core and the single-owner bearer token check."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import HTTPException, Request, WebSocket

if TYPE_CHECKING:
    from jarvis_core.app import Core


def core_of(request: Request) -> Core:
    return cast("Core", request.app.state.core)


def core_of_ws(ws: WebSocket) -> Core:
    return cast("Core", ws.app.state.core)


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token") or request.cookies.get("jarvis_token")


def token_matches(given: str | None, expected: str) -> bool:
    """Constant-time bearer comparison — the host's BearerAuth already does this; the core's own
    check used ``==``, which returns as soon as two bytes differ."""
    return given is not None and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


async def require_token(request: Request) -> None:
    core = core_of(request)
    expected = core.config.token
    if expected is None:
        return
    if not token_matches(_token_from(request), expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


def ws_token_ok(ws: WebSocket) -> bool:
    expected = core_of_ws(ws).config.token
    if expected is None:
        return True
    return token_matches(ws.query_params.get("token"), expected) or token_matches(
        ws.cookies.get("jarvis_token"), expected
    )


AgentAccess = Literal["read", "write", "none"]


def agent(
    access: AgentAccess,
    *,
    risk: str | None = None,
    risk_when: dict[str, list[Any]] | None = None,
    prefer: str | None = None,
) -> dict[str, Any]:
    """Whether Jarvis may call this route itself through jarvis.api_* (docs/stories/14_agent_admin.md).

    A declaration on the route, read back from ``app.openapi()``, so a route added later forces a
    yes or no in the same diff (tests/test_agent_routes.py fails on an /api route without one) and
    nobody keeps a list. ``read`` = GET it; ``write`` = call it; ``none`` = never (credentials,
    approvals, media streams). ``risk`` makes every call ask first (a DELETE is "delete" by
    nature); ``risk_when`` only when a body field has one of the values (bulk delete, not bulk
    archive). ``prefer`` names the precise tool to use instead.
    """
    return {"x-jarvis-agent": {"access": access, "risk": risk, "risk_when": risk_when, "prefer": prefer}}
