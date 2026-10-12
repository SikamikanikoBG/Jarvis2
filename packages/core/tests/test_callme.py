"""Jarvis calls back (docs/stories/11_call_me.md): the run flag, the ring, web push, the fallback."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Callable
from pathlib import Path

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.features.callme import CallMe, Ring, plain
from jarvis_core.features.notify import NotifyTools
from jarvis_core.features.webpush import PushDevice, PushIdentity, b64url, b64url_decode
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import RoleName, RunStatus
from tests.conftest import Harness

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


class Browser:
    """The other end of a subscription: holds the keys a real PushManager would."""

    def __init__(self, endpoint: str = ENDPOINT) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        pub = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.device = PushDevice(endpoint=endpoint, p256dh=b64url(pub), auth=b64url(self.auth), name="phone")

    def open(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


def _transport(log: list[httpx.Request], status: Callable[[httpx.Request], int] = lambda r: 201) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        log.append(request)
        return httpx.Response(status(request))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _wired(harness: Harness, log: list[httpx.Request], **kw) -> CallMe:
    """The harness's core with a recorded carrier and a ring that does not wait."""
    harness.core.notify = NotifyTools(lambda: harness.core.settings, client=_transport(log, **kw), db=harness.core.db)
    harness.core.callme = CallMe(harness.core, gather_s=0)
    await harness.core.callme.start()
    return harness.core.callme


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


async def test_vapid_is_a_valid_es256_token_and_the_key_survives_a_restart(harness: Harness):
    identity = await PushIdentity.load(harness.core.db)
    assert (await PushIdentity.load(harness.core.db)).public_key == identity.public_key
    header = identity.authorization(ENDPOINT, "mailto:x@y.z")
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert key == identity.public_key
    head, body, sig = token.split(".")
    claims = json.loads(b64url_decode(body))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:x@y.z"
    raw = b64url_decode(sig)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(key))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if forged


async def test_a_run_with_the_bell_on_rings_every_device_with_its_reply(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    phone, laptop = Browser(), Browser("https://web.push.apple.com/xyz")
    await harness.core.notify.devices.add(phone.device)
    await harness.core.notify.devices.add(laptop.device)
    conv = await harness.core.store.create_conversation(title="Q3 report")
    sub = harness.subscribe(conv.id)
    harness.chat.push(FakeTurn(text="**Done.** The report is in [Drive](https://x.y/z). It has 12 pages. Fourth one."))
    run, _ = await harness.core.engine.create_run(text="build the report", conversation_id=conv.id, call_me=True)
    await harness.wait_for(sub, "run.done")
    await _until(lambda: len(sent) == 2)

    to_phone = next(r for r in sent if str(r.url) == ENDPOINT)
    assert to_phone.headers["Content-Encoding"] == "aes128gcm" and to_phone.headers["Urgency"] == "high"
    assert to_phone.headers["Authorization"].startswith("vapid t=")
    ring = phone.open(to_phone.content)
    assert ring["type"] == "call" and ring["kind"] == "done"
    assert ring["conversation_id"] == conv.id and ring["run_id"] == run.id
    assert ring["path"] == f"/c/{conv.id}?ring={run.id}"
    assert ring["title"] == "Jarvis is done · Q3 report"
    # What the answered call reads: words, not markdown, and the start of the reply only.
    assert ring["say"] == "Done. The report is in Drive. It has 12 pages."
    assert laptop.open(next(r for r in sent if r is not to_phone).content)["run_id"] == run.id
    assert all(d.last_ok_at for d in await harness.core.notify.devices.list())


async def _done(harness: Harness, run_id: str) -> None:
    async def finished() -> bool:
        run = await harness.core.store.get_run(run_id)
        return run is not None and run.status.terminal

    async with asyncio.timeout(5):
        while not await finished():
            await asyncio.sleep(0.02)


async def test_by_default_a_chat_rings_when_nobody_is_watching_it(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    await harness.core.notify.devices.add(Browser().device)
    conv = await harness.core.store.create_conversation()
    harness.chat.push(FakeTurn(text="Done."))
    run, _ = await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    assert run.call_me and run.call_me_default
    await _done(harness, run.id)
    await _until(lambda: len(sent) == 1)


async def test_the_default_follows_the_settings_and_the_bell_turned_off_wins(harness: Harness):
    from jarvis_proto import Channel, RunKind

    assert harness.core.settings.call_me_default == [RunKind.CHAT, RunKind.SCHEDULED]
    sched, _ = await harness.core.engine.create_run(text="digest", kind=RunKind.SCHEDULED)
    triage, _ = await harness.core.engine.create_run(text="mail", kind=RunKind.TRIAGE)
    voice, _ = await harness.core.engine.create_run(text="hi", channel=Channel.VOICE)
    off, _ = await harness.core.engine.create_run(text="quiet", call_me=False)
    assert (sched.call_me, triage.call_me, voice.call_me, off.call_me) == (True, False, False, False)
    harness.enable(call_me_default=[RunKind.TRIAGE])
    chat, _ = await harness.core.engine.create_run(text="x")
    triage2, _ = await harness.core.engine.create_run(text="mail", kind=RunKind.TRIAGE)
    assert (chat.call_me, triage2.call_me) == (False, True)


async def test_a_default_ring_stays_quiet_while_he_watches_the_chat(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    await harness.core.notify.devices.add(Browser().device)
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.chat.push(FakeTurn(text="ok"))
    await harness.core.engine.create_run(text="hi", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    await asyncio.sleep(0.1)
    assert sent == []


async def test_the_bell_pressed_while_the_run_works_and_taken_back(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    await harness.core.notify.devices.add(Browser().device)
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.chat.push(FakeTurn(text="slow answer", token_delay_s=0.05))
    run, _ = await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "run.started")
    assert await harness.core.engine.set_call_me(run.id, True)
    seen = await harness.wait_for(sub, "run.updated")
    assert seen[-1].run.call_me is True
    await harness.wait_for(sub, "run.done")
    await _until(lambda: len(sent) == 1)
    assert (await harness.core.store.get_run(run.id)).call_me is True
    # A finished run cannot be rung any more.
    assert not await harness.core.engine.set_call_me(run.id, False)


async def test_the_run_asks_for_the_ring_itself(harness: Harness):
    from jarvis_proto import ToolCall

    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    await harness.core.notify.devices.add(Browser().device)
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="jarvis.call_me", arguments={})]))
    harness.chat.push(FakeTurn(text="I will call you."))
    run, _ = await harness.core.engine.create_run(text="call me when done", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    await _until(lambda: len(sent) == 1)
    assert (await harness.core.store.get_run(run.id)).call_me


async def test_a_failed_run_rings_and_says_why(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    phone = Browser()
    await harness.core.notify.devices.add(phone.device)
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.chat.push(FakeTurn(fail_before_first_byte=True))
    harness.chat.push(FakeTurn(fail_before_first_byte=True))
    harness.chat.push(FakeTurn(fail_before_first_byte=True))
    harness.chat.push(FakeTurn(fail_before_first_byte=True))
    run, _ = await harness.core.engine.create_run(text="x", conversation_id=conv.id, call_me=True)
    await harness.wait_for(sub, "run.failed", timeout=30)
    await _until(lambda: len(sent) == 1)
    ring = phone.open(sent[0].content)
    assert ring["kind"] == "failed" and ring["run_id"] == run.id and ring["say"]
    assert (await harness.core.store.get_run(run.id)).status is RunStatus.FAILED


async def test_a_gone_device_is_forgotten_and_discord_carries_the_ring(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent, status=lambda r: 410 if "fcm" in str(r.url) else 204)
    harness.enable(
        discord_webhook_url="https://discord.com/api/webhooks/1/abc", public_url="https://ardi.example.ts.net:8444"
    )
    await harness.core.notify.devices.add(Browser().device)
    carrier = await harness.core.callme.ring([Ring("run_1", "conv_1", "done", "Q3", "All done.")])
    assert carrier == "discord"
    assert await harness.core.notify.devices.list() == []
    discord = json.loads(sent[-1].content)["content"]
    assert "Jarvis is done · Q3" in discord and "All done." in discord
    assert "https://ardi.example.ts.net:8444/c/conv_1?ring=run_1" in discord


async def test_two_runs_ending_together_ring_once(harness: Harness):
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    harness.core.callme.gather_s = 0.3
    phone = Browser()
    await harness.core.notify.devices.add(phone.device)
    subs, runs = [], []
    for title in ("Mail", "Report"):
        conv = await harness.core.store.create_conversation(title=title)
        subs.append(harness.subscribe(conv.id))
        harness.chat.push(FakeTurn(text=f"{title} finished."))
        run, _ = await harness.core.engine.create_run(text=title, conversation_id=conv.id, call_me=True)
        runs.append(run)
    for sub in subs:
        await harness.wait_for(sub, "run.done")
    await asyncio.sleep(0.6)
    assert len(sent) == 1
    ring = phone.open(sent[0].content)
    assert ring["title"] == "Jarvis: 2 tasks ended"
    assert "Mail finished." in ring["say"] and "Report finished." in ring["say"]


def test_plain_says_words_not_markdown():
    assert plain("# Title\n- **bold** item\n```py\ncode\n```\nSee `x` at https://a.b/c.") == "Title bold item See x at"
    assert plain("One. Two! Three? Four.") == "One. Two! Three?"


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.core = core  # type: ignore[attr-defined]
        yield c


def test_devices_api(client: TestClient):
    h = {"Authorization": "Bearer secret"}
    assert client.get("/api/push").status_code == 401
    state = client.get("/api/push", headers=h).json()
    assert len(base64.urlsafe_b64decode(state["public_key"] + "==")) == 65 and state["devices"] == []
    # The key is the core's own and does not change between calls.
    assert client.get("/api/push", headers=h).json()["public_key"] == state["public_key"]
    sub = {"endpoint": ENDPOINT, "keys": {"p256dh": Browser().device.p256dh, "auth": "c2VjcmV0c2VjcmV0MTIzNA"}}
    assert client.post("/api/push/devices", json={**sub, "name": "Pixel"}, headers=h).status_code == 204
    assert client.post("/api/push/devices", json={**sub, "name": "Pixel 9"}, headers=h).status_code == 204
    devices = client.get("/api/push", headers=h).json()["devices"]
    assert [(d["endpoint"], d["name"]) for d in devices] == [(ENDPOINT, "Pixel 9")]
    # The VAPID private key is never in the settings the UI reads.
    assert "PRIVATE KEY" not in client.get("/api/settings", headers=h).text
    assert client.post("/api/push/devices/forget", json={"endpoint": ENDPOINT}, headers=h).status_code == 204
    assert client.get("/api/push", headers=h).json()["devices"] == []
    # No device and no Discord: the test ring says nothing could carry it.
    assert client.post("/api/push/test", headers=h).json() == {"carrier": None}


def test_run_create_and_run_call_me_over_the_socket(client: TestClient):
    core: Core = client.core  # type: ignore[attr-defined]
    fake: FakeAdapter = core.adapters.fakes[RoleName.CHAT]  # type: ignore[assignment]
    fake.push(FakeTurn(text="done"))
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_json({"type": "run.create", "text": "hi", "call_me": True})
        run_id = None
        while True:
            ev = ws.receive_json()
            if ev.get("type") == "run.queued":
                run_id = ev["run_id"]
            if ev.get("type") == "run.done":
                break
    run = client.get(f"/api/runs/{run_id}", headers={"Authorization": "Bearer secret"}).json()
    assert run["call_me"] is True


async def test_one_broken_device_does_not_stop_the_ring_and_is_forgotten(harness: Harness):
    """Alpha F-001: undecodable keys made the whole ring a 500."""
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    good = Browser()
    await harness.core.notify.devices.add(PushDevice(endpoint="https://fcm.googleapis.com/x", p256dh="!!!", auth="x"))
    await harness.core.notify.devices.add(good.device)
    assert await harness.core.callme.ring([Ring("run_1", "conv_1", "done", "Q3", "Done.")]) == "push"
    assert [str(r.url) for r in sent] == [ENDPOINT]
    assert [d.endpoint for d in await harness.core.notify.devices.list()] == [ENDPOINT]


async def test_a_default_ring_waits_for_him_to_look_away(harness: Harness):
    """Alpha F-005: a background tab or a locked phone keeps its socket; only a visible page
    counts as watching the chat."""
    sent: list[httpx.Request] = []
    await _wired(harness, sent)
    await harness.core.notify.devices.add(Browser().device)
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    sub.visible = False  # the app said: hidden
    harness.chat.push(FakeTurn(text="Done."))
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    await _until(lambda: len(sent) == 1)


def test_devices_api_refuses_what_cannot_be_pushed_to(client: TestClient):
    """Alpha F-002: http and broken keys were stored; a huge name too."""
    h = {"Authorization": "Bearer secret"}
    key = Browser().device.p256dh
    ok = {"endpoint": ENDPOINT, "keys": {"p256dh": key, "auth": "c2VjcmV0c2VjcmV0MTIzNA"}}
    for bad in (
        {**ok, "endpoint": "http://evil.local/x"},
        {**ok, "keys": {"p256dh": "!!!", "auth": "x"}},
        {**ok, "keys": {"p256dh": key, "auth": "c2hvcnQ"}},
        {**ok, "name": "x" * 1000},
    ):
        assert client.post("/api/push/devices", json=bad, headers=h).status_code == 422, bad
    assert client.get("/api/push", headers=h).json()["devices"] == []
    assert client.post("/api/push/devices", json=ok, headers=h).status_code == 204


def test_presence_over_the_socket(client: TestClient):
    core: Core = client.core  # type: ignore[attr-defined]
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_json({"type": "subscribe", "conversation_id": "conv_x"})
        ws.send_json({"type": "ping"})
        ws.receive_json()
        assert core.bus.has_viewers("conv_x")
        ws.send_json({"type": "presence", "visible": False})
        ws.send_json({"type": "ping"})
        ws.receive_json()
        assert core.bus.has_subscribers("conv_x") and not core.bus.has_viewers("conv_x")


async def test_a_scheduled_task_says_whether_it_rings(harness: Harness):
    """Per task: "default" follows settings.call_me_default, "always" and "never" do not."""
    core = harness.core
    harness.chat.push(FakeTurn(text="a"), FakeTurn(text="b"), FakeTurn(text="c"))
    rings = {}
    for mode in ("default", "always", "never"):
        sched = await core.schedules.create(name=mode, prompt="go", cron="0 9 * * 1", tz="UTC", call_me=mode)
        assert sched.call_me == mode
        run_id, _ = await core.scheduler.run_now(sched)
        run = await core.store.get_run(run_id)
        rings[mode] = (run.call_me, run.call_me_default)
    assert rings == {"default": (True, True), "always": (True, False), "never": (False, False)}
    # Changed later, and back to default.
    patched = await core.schedules.update(sched.id, call_me="default")
    assert patched is not None and patched.call_me == "default"


async def test_a_task_set_never_to_ring_cannot_ring_itself(harness: Harness):
    from jarvis_proto import ToolCall

    core = harness.core
    sched = await core.schedules.create(
        name="Quiet", prompt="call me when done", cron="0 9 * * 1", tz="UTC", call_me="never"
    )
    harness.chat.push(FakeTurn(tool_calls=[ToolCall(id="c1", name="jarvis.call_me", arguments={})]))
    harness.chat.push(FakeTurn(text="Done."))
    run_id, conv_id = await core.scheduler.run_now(sched)
    await _done(harness, run_id)
    assert (await core.store.get_run(run_id)).call_me is False
    tool_msgs = [m for m in await core.store.list_messages(conv_id) if m.role == "tool"]
    assert tool_msgs and "never to ring" in tool_msgs[0].content
