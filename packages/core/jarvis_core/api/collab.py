"""Collab REST (keys, message), pairing, identity."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from jarvis_core.api.deps import core_of, require_token
from jarvis_proto import CollabKey

router = APIRouter(prefix="/api")
owner_router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@dataclass(slots=True)
class Identity:
    owner: bool
    key: CollabKey | None


async def identify(request: Request) -> Identity:
    """Owner token → owner; collab key → that key; nothing configured → owner."""
    core = core_of(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else (request.query_params.get("token") or "")
    if core.config.token is None and not token:
        return Identity(owner=True, key=None)
    if core.config.token is not None and token == core.config.token:
        return Identity(owner=True, key=None)
    key = await core.collab_keys.verify(token)
    if key is None:
        raise HTTPException(401, "invalid or missing token")
    return Identity(owner=False, key=key)


class KeyCreate(BaseModel):
    name: str


class CollabMessage(BaseModel):
    text: str
    conversation_id: str | None = None


@owner_router.get("/collab/keys", response_model=list[CollabKey])
async def list_keys(request: Request) -> list[CollabKey]:
    return await core_of(request).collab_keys.list()


@owner_router.post("/collab/keys", status_code=201)
async def create_key(request: Request, body: KeyCreate) -> dict[str, Any]:
    plain, key = await core_of(request).collab_keys.create(body.name)
    return {"key": plain, "id": key.id, "name": key.name, "created_at": key.created_at}


@owner_router.delete("/collab/keys/{key_id}", status_code=204)
async def delete_key(request: Request, key_id: str) -> Response:
    await core_of(request).collab_keys.delete(key_id)
    return Response(status_code=204)


@router.post("/collab/message")
async def collab_message(request: Request, body: CollabMessage, who: Identity = Depends(identify)) -> dict[str, Any]:
    from jarvis_core.features.collab import run_and_wait

    reply, conv_id, run_id, status = await run_and_wait(
        core_of(request), text=body.text, key=who.key, conversation_id=body.conversation_id
    )
    return {"reply": reply, "conversation_id": conv_id, "run_id": run_id, "status": status}


@router.get("/whoami")
async def whoami(who: Identity = Depends(identify)) -> dict[str, Any]:
    return {"owner": who.owner, "key_name": who.key.name if who.key else None}


@owner_router.get("/pair")
async def pair(request: Request) -> dict[str, str]:
    """URL + QR for the phone. The URL carries the owner token; show it only to the owner."""
    import qrcode
    import qrcode.image.svg

    core = core_of(request)
    base = str(request.base_url).rstrip("/")
    url = f"{base}/?token={core.config.token}" if core.config.token else f"{base}/"
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return {"url": url, "qr_svg": buf.getvalue().decode("utf-8")}
