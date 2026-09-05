"""Request-scoped access to the core and the single-owner bearer token check."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

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


async def require_token(request: Request) -> None:
    core = core_of(request)
    expected = core.config.token
    if expected is None:
        return
    if _token_from(request) != expected:
        raise HTTPException(status_code=401, detail="invalid or missing token")


def ws_token_ok(ws: WebSocket) -> bool:
    expected = core_of_ws(ws).config.token
    if expected is None:
        return True
    return ws.query_params.get("token") == expected or ws.cookies.get("jarvis_token") == expected
