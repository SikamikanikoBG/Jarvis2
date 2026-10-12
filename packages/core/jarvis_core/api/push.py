"""The devices Jarvis can ring (docs/stories/11_call_me.md): web push subscriptions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jarvis_core.api.deps import core_of, require_token
from jarvis_core.features.callme import Ring
from jarvis_core.features.webpush import PushDevice

router = APIRouter(prefix="/api/push", dependencies=[Depends(require_token)])


class DeviceKeys(BaseModel):
    p256dh: str
    auth: str


class DeviceIn(BaseModel):
    """What ``PushSubscription.toJSON()`` gives, plus a name to tell the devices apart."""

    endpoint: str = Field(max_length=2048)
    keys: DeviceKeys
    name: str = Field(default="", max_length=80)


class DeviceOut(BaseModel):
    endpoint: str
    name: str
    created_at: str
    last_ok_at: str | None = None


class PushState(BaseModel):
    #: What a browser subscribes with (the core's VAPID key, base64url).
    public_key: str
    devices: list[DeviceOut]


class Forget(BaseModel):
    endpoint: str


class TestRing(BaseModel):
    #: The carrier that took it: "push", "discord", or None when neither could.
    carrier: str | None


@router.get("", response_model=PushState)
async def state(request: Request) -> PushState:
    notify = core_of(request).notify
    devices = await notify.devices.list() if notify.devices else []
    return PushState(
        public_key=(await notify.identity()).public_key,
        devices=[
            DeviceOut(endpoint=d.endpoint, name=d.name, created_at=d.created_at, last_ok_at=d.last_ok_at)
            for d in devices
        ],
    )


@router.post("/devices", status_code=204)
async def add_device(request: Request, body: DeviceIn) -> Response:
    notify = core_of(request).notify
    device = PushDevice(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth, name=body.name.strip())
    try:
        device.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if notify.devices is not None:
        await notify.devices.add(device)
    return Response(status_code=204)


@router.post("/devices/forget", status_code=204)
async def forget_device(request: Request, body: Forget) -> Response:
    notify = core_of(request).notify
    if notify.devices is not None:
        await notify.devices.remove(body.endpoint)
    return Response(status_code=204)


@router.post("/test", response_model=TestRing)
async def test_ring(request: Request) -> TestRing:
    """Ring now, the way a finished task rings: answering opens a call in a new chat."""
    ring = Ring(run_id="", conversation_id="", kind="done", chat="test", say="This is how I ring when a task is done.")
    return TestRing(carrier=await core_of(request).callme.ring([ring]))
