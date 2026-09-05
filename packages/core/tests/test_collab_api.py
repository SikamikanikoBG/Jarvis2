"""Collab keys, /api/collab/message, the OpenAI-compatible façade, pairing, STT endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
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
    core = Core(CoreConfig(home=tmp_path, token="owner"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.patch("/api/settings", json=fake_settings().model_dump(mode="json"), headers=OWNER)
        c.fake = fake  # type: ignore[attr-defined]
        c.core = core  # type: ignore[attr-defined]
        yield c


OWNER = {"Authorization": "Bearer owner"}


def test_keys_and_collab_message(client: TestClient):
    r = client.post("/api/collab/keys", json={"name": "claude-code"}, headers=OWNER)
    assert r.status_code == 201
    plain, key_id = r.json()["key"], r.json()["id"]
    assert plain.startswith("jk_")
    assert [k["name"] for k in client.get("/api/collab/keys", headers=OWNER).json()] == ["claude-code"]
    assert client.get("/api/collab/keys").status_code == 401  # owner only

    key_hdr = {"Authorization": f"Bearer {plain}"}
    assert client.get("/api/whoami", headers=key_hdr).json() == {"owner": False, "key_name": "claude-code"}
    assert client.get("/api/whoami", headers=OWNER).json() == {"owner": True, "key_name": None}
    assert client.get("/api/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401

    client.fake.push(FakeTurn(text="Hello from Jarvis"))  # type: ignore[attr-defined]
    r = client.post("/api/collab/message", json={"text": "ping"}, headers=key_hdr)
    body = r.json()
    assert r.status_code == 200 and body["reply"] == "Hello from Jarvis" and body["status"] == "done"
    conv = client.get(f"/api/conversations/{body['conversation_id']}", headers=OWNER).json()
    assert conv["kind"] == "collab" and conv["folder_label"] == "claude-code" and conv["folder_key"] == key_id

    # Same conversation continues.
    client.fake.push(FakeTurn(text="Second"))  # type: ignore[attr-defined]
    r = client.post(
        "/api/collab/message", json={"text": "again", "conversation_id": body["conversation_id"]}, headers=key_hdr
    )
    assert r.json()["conversation_id"] == body["conversation_id"] and r.json()["reply"] == "Second"

    client.delete(f"/api/collab/keys/{key_id}", headers=OWNER)
    assert client.get("/api/whoami", headers=key_hdr).status_code == 401


def test_openai_compatible_facade_streaming_and_not(client: TestClient):
    assert client.get("/v1/models", headers=OWNER).json()["data"][0]["id"] == "jarvis"
    client.fake.push(FakeTurn(text="The answer is 42", reasoning="thinking"))  # type: ignore[attr-defined]
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "jarvis",
            "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "meaning?"}],
        },
        headers=OWNER,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "The answer is 42"
    assert body["choices"][0]["message"]["reasoning"] == "thinking"
    conv_id = r.headers["X-Jarvis-Conversation"]

    client.fake.push(FakeTurn(text="streamed reply here"))  # type: ignore[attr-defined]
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "jarvis", "stream": True, "messages": [{"role": "user", "content": "more"}]},
        headers={**OWNER, "X-Jarvis-Conversation": conv_id},
    ) as s:
        assert s.headers["content-type"].startswith("text/event-stream")
        assert s.headers["X-Jarvis-Conversation"] == conv_id
        frames = [line for line in s.iter_lines() if line.startswith("data:")]
    assert frames[-1] == "data: [DONE]"
    text = "".join(
        json.loads(f[5:])["choices"][0]["delta"].get("content", "") for f in frames[:-1] if f != "data: [DONE]"
    )
    assert text == "streamed reply here"
    finish = json.loads(frames[-2][5:])["choices"][0]["finish_reason"]
    assert finish == "stop"
    msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=OWNER).json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]


def test_pair_returns_qr_with_token(client: TestClient):
    r = client.get("/api/pair", headers=OWNER)
    assert r.status_code == 200
    body = r.json()
    assert (body["url"].endswith("/?token=owner") and body["qr_svg"].lstrip().startswith("<?xml")) or "<svg" in body[
        "qr_svg"
    ]
    assert client.get("/api/pair").status_code == 401


def test_stt_endpoint_errors_loudly_and_transcribes_via_backend(client: TestClient):
    r = client.post("/api/stt", files={"audio": ("a.webm", b"\x00\x01", "audio/webm")}, headers=OWNER)
    assert r.status_code == 502 and "no STT backend" in r.json()["detail"]

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body_has_file"] = b'name="file"' in request.read()
        return httpx.Response(
            200,
            json={
                "text": "  Здравей, Jarvis  ",
                "language": "bg",
                "segments": [{"start": 0.0, "end": 1.2, "text": "Здравей"}],
            },
            headers={"X-Whisper-Upstream": "10.0.0.1:9100, 10.0.0.2:9100"},
        )

    core = client.core  # type: ignore[attr-defined]
    core.transcriber._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.patch("/api/settings", json={"stt_url": "http://whisper:9110"}, headers=OWNER)
    r = client.post(
        "/api/stt", data={"language": "bg"}, files={"audio": ("a.webm", b"\x00\x01", "audio/webm")}, headers=OWNER
    )
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Здравей, Jarvis" and body["language"] == "bg"
    assert seen["url"] == "http://whisper:9110/v1/audio/transcriptions" and seen["body_has_file"]
    # A proxy fail-over is reported, never hidden.
    assert body["warning"] and "fallback upstream 10.0.0.2:9100" in body["warning"]
