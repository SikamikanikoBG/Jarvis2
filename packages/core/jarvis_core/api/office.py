"""The Office (features/office.py): its socket, which character is which conversation, and the
static webview at /pixel-office/ (the SPA owns /office)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from jarvis_core.api.deps import core_of, core_of_ws, require_token, ws_token_ok

log = logging.getLogger(__name__)
DROPPED_CHECK_S = 15.0
router = APIRouter()
rest = APIRouter(prefix="/api/office", dependencies=[Depends(require_token)])


@rest.get("/agents")
async def office_agents(request: Request) -> dict[str, Any]:
    office = core_of(request).office
    return {"built": office.dist is not None, "agents": office.view()}


@router.websocket("/pixel-office/ws")
async def office_socket(ws: WebSocket) -> None:
    if not ws_token_ok(ws):
        await ws.close(code=4401, reason="unauthorized")
        return
    office = core_of_ws(ws).office
    await ws.accept()
    queue = office.connect()

    # The handshake goes through the same queue as the broadcasts, so nothing that happens while
    # it is being built can overtake it.
    async def enqueue(message: dict[str, Any]) -> None:
        queue.put_nowait(message)

    async def writer() -> None:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), DROPPED_CHECK_S)
            except TimeoutError:
                if not office.connected(queue):
                    # Dropped for falling behind: close, and the webview reconnects to a fresh office.
                    await ws.close(code=1013, reason="fell behind")
                    return
                continue
            await ws.send_text(json.dumps(message, separators=(",", ":")))

    writer_task = asyncio.create_task(writer())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                await office.handle(message, enqueue)
    except WebSocketDisconnect:
        pass
    finally:
        office.disconnect(queue)
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer_task


def mount_office(app: FastAPI, core: Any) -> None:
    """Serve the built webview at /pixel-office/ (before the SPA catch-all claims the path)."""
    app.include_router(router)
    app.include_router(rest)
    dist = core.config.resolve_office_dist()
    if dist is not None:
        app.mount("/pixel-office", StaticFiles(directory=dist, html=True), name="pixel-office")
