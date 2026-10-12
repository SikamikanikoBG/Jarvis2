"""Web Push, the standard: how a page that is not open still reaches a phone.

A browser that turns push on hands the core a *subscription*: the URL of its push service (Google
for Chrome and Brave, Apple for Safari, Mozilla for Firefox) and two keys. To reach it the core
POSTs to that URL a payload sealed for those keys (RFC 8291, aes128gcm) and signed with its own
identity (VAPID, RFC 8292: an ES256 JWT). The push service wakes the browser's service worker,
which shows the notification. The push service carries the box but cannot open it.

This module is only that transport: the identity, the devices and one send. *What* is sent and
*when* is notify's and callme's business.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from jarvis_core.db import Database

log = logging.getLogger(__name__)

# How long a push service keeps a ring for a phone that is off or out of coverage. A ring that
# arrives an hour late is still worth having ("it finished"), a day late it is noise.
TTL_S = 3600
# send()'s answer for a device that can never be pushed to (its keys do not work): forget it.
UNUSABLE = -1
# The VAPID token's lifetime; the standard allows up to 24 h.
_JWT_LIFETIME_S = 12 * 3600


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


class PushIdentity:
    """The core's VAPID key: made once, kept in ``push_identity``.

    A browser's subscription is bound to the public half, so losing this key silently orphans
    every subscribed device. That is why it lives in its own table, not in the settings.
    """

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        self._key = key
        self.public_key = b64url(_public_bytes(key))

    @classmethod
    async def load(cls, db: Database) -> PushIdentity:
        row = await db.fetchone("SELECT private_pem FROM push_identity WHERE id = 1")
        if row is not None:
            key = serialization.load_pem_private_key(row["private_pem"].encode("ascii"), password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey):
                return cls(key)
            log.warning("push identity is not an EC key; making a new one (devices must re-subscribe)")
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode("ascii")
        await db.execute(
            "INSERT OR REPLACE INTO push_identity(id, private_pem, created_at) VALUES (1, ?, ?)",
            (pem, datetime.now(UTC).isoformat()),
        )
        return cls(key)

    def authorization(self, endpoint: str, subject: str) -> str:
        """The ``Authorization`` header for one push to ``endpoint``."""
        parts = urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + _JWT_LIFETIME_S, "sub": subject}
        head = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64url(json.dumps(claims, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self._key.sign(f"{head}.{body}".encode("ascii"), ec.ECDSA(hashes.SHA256())))
        signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={head}.{body}.{signature}, k={self.public_key}"


@dataclass(slots=True)
class PushDevice:
    endpoint: str
    p256dh: str  # the browser's P-256 public key, base64url
    auth: str  # its 16-byte auth secret, base64url
    name: str = ""
    created_at: str = ""
    last_ok_at: str | None = None

    def check(self) -> None:
        """ValueError unless this is a subscription a push can be sealed for and sent to."""
        if urlsplit(self.endpoint).scheme != "https" or not urlsplit(self.endpoint).netloc:
            raise ValueError("a push endpoint is an https:// URL")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(self.p256dh))
            auth = b64url_decode(self.auth)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("keys.p256dh is not a P-256 public key") from exc
        if len(auth) != 16:
            raise ValueError("keys.auth is not a 16-byte secret")


class PushDevices:
    """The devices that ring (``push_devices``)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[PushDevice]:
        rows = await self._db.fetchall("SELECT * FROM push_devices ORDER BY created_at")
        return [
            PushDevice(
                endpoint=r["endpoint"],
                p256dh=r["p256dh"],
                auth=r["auth"],
                name=r["name"],
                created_at=r["created_at"],
                last_ok_at=r["last_ok_at"],
            )
            for r in rows
        ]

    async def add(self, device: PushDevice) -> None:
        # The same browser subscribing again (a new key, a renamed device) replaces its row.
        await self._db.execute(
            "INSERT INTO push_devices(endpoint, p256dh, auth, name, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, name=excluded.name",
            (device.endpoint, device.p256dh, device.auth, device.name, datetime.now(UTC).isoformat()),
        )

    async def remove(self, endpoint: str) -> None:
        await self._db.execute("DELETE FROM push_devices WHERE endpoint = ?", (endpoint,))

    async def mark_ok(self, endpoint: str) -> None:
        await self._db.execute(
            "UPDATE push_devices SET last_ok_at = ? WHERE endpoint = ?", (datetime.now(UTC).isoformat(), endpoint)
        )


def seal(payload: dict[str, Any], device: PushDevice) -> bytes:
    """The payload encrypted for one device (RFC 8291). A fresh key per message, as required."""
    return http_ece.encrypt(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=b64url_decode(device.p256dh),
        auth_secret=b64url_decode(device.auth),
        version="aes128gcm",
    )


async def send(
    client: httpx.AsyncClient,
    identity: PushIdentity,
    device: PushDevice,
    payload: dict[str, Any],
    *,
    subject: str,
    topic: str | None = None,
) -> int:
    """One push to one device; the push service's HTTP status (0 = it could not be reached).

    ``topic`` lets a newer push replace one the phone has not received yet, so a phone that
    was off gets the latest ring, not a pile of them."""
    headers = {
        "Authorization": identity.authorization(device.endpoint, subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_S),
        "Urgency": "high",
    }
    if topic:
        headers["Topic"] = topic
    try:
        body = seal(payload, device)
    except (ValueError, binascii.Error) as exc:
        log.warning("push to %s: its keys do not work (%s)", urlsplit(device.endpoint).netloc, exc)
        return UNUSABLE
    try:
        resp = await client.post(device.endpoint, content=body, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("push to %s failed: %s", urlsplit(device.endpoint).netloc, type(exc).__name__)
        return 0
    if resp.status_code >= 300:
        log.warning("push to %s: HTTP %s %s", urlsplit(device.endpoint).netloc, resp.status_code, resp.text[:200])
    return resp.status_code
