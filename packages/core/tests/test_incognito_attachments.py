"""An incognito chat's attachments are never on the disk - not the photo, not its thumbnail, not
the transcript of a clip, not the text of a pasted note. Memory only, gone with the chat or the
process, and told to stay out of every cache on the way to the screen.

2026-09-13, "INCOGNITO MEANS INCOGNITO": a clip recorded into an incognito chat was found in
data/attachments on the core, with its transcript in the row. These are the tests that would
have failed that morning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import INCOGNITO_TITLE, RoleName
from tests.conftest import Harness
from tests.test_attachments import png


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


async def test_an_incognito_upload_never_touches_the_disk(harness: Harness):
    core = harness.core
    root = core.config.home / "attachments"
    before = _files_under(root)

    # The first message of a new incognito chat uploads before the chat exists: the flag is
    # all the core has to go on.
    att = await core.attachments.add_file(
        data=png(900, 600), filename="IMG_secret.png", mime="image/png", conversation_id=None, incognito=True
    )
    stored = await core.attachments.get(att.id)
    assert stored is not None and stored.private and stored.path is None
    assert _files_under(root) == before, "nothing new on disk"
    row = await core.db.fetchone("SELECT path, text FROM attachments WHERE id = ?", (att.id,))
    assert row is not None and row["path"] is None and row["text"] is None

    # The thumbnail and the model's data URL both come from memory - and leave no cache file.
    thumb = await core.attachments.thumbnail(att.id)
    assert thumb is not None and thumb[1] == "image/jpeg"
    url = await core.attachments.data_url(att.id)
    assert url and url.startswith("data:image/png;base64,")
    assert _files_under(root) == before

    # Pasted text in an incognito chat: no text column either, but the model still reads it.
    conv = await core.store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    note = await core.attachments.add_text(text="the pin is 4711", name="note", conversation_id=conv.id)
    row = await core.db.fetchone("SELECT text FROM attachments WHERE id = ?", (note.id,))
    assert row is not None and row["text"] is None
    got = await core.attachments.get(note.id)
    assert got is not None and got.private and got.attachment.text == "the pin is 4711"

    # Sent with a message, the picture still reaches the model.
    harness.chat.push(FakeTurn(text="A square."))
    await core.engine.create_run(text="look", conversation_id=conv.id, attachment_ids=[att.id, note.id])
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    sent = harness.chat.calls[-1][0]
    user = [m for m in sent if m.role.value == "user" and not m.name][-1]
    assert user.attachments and user.attachments[0].data_url
    assert user.attachments[0].data_url.startswith("data:image/")
    assert _files_under(root) == before

    # Deleting the chat lets the memory go too.
    await core.delete_conversation(conv.id)
    assert await core.attachments.get(att.id) is None
    assert not core.attachments.is_private(att.id)


async def test_a_video_in_an_incognito_chat_keeps_its_transcript_in_memory_only(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    from tests.test_attachments import clip

    core = harness.core

    async def fake_transcribe(audio: bytes, *, filename: str = "", mime: str = "") -> object:
        class R:
            text = "what was said in the clip"
            language = "bg"

        return R()

    monkeypatch.setattr(core.transcriber, "transcribe", fake_transcribe)
    root = core.config.home / "attachments"
    before = _files_under(root)
    att = await core.attachments.add_file(
        data=clip(3, 10, audio=True),
        filename="20260913_203501.mp4",
        mime="video/mp4",
        conversation_id=None,
        incognito=True,
    )
    assert att.text == "what was said in the clip" and att.meta["audio"] == "transcribed"
    row = await core.db.fetchone("SELECT path, text FROM attachments WHERE id = ?", (att.id,))
    assert row is not None and row["path"] is None and row["text"] is None
    assert _files_under(root) == before
    # The poster frame for the transcript, from memory.
    thumb = await core.attachments.thumbnail(att.id)
    assert thumb is not None and thumb[1] == "image/jpeg"
    assert _files_under(root) == before


async def test_a_file_bound_into_an_incognito_chat_without_the_flag_is_pulled_off_the_disk(harness: Harness):
    """An older client (or a script) that uploads without saying `incognito` and then sends the
    message into an incognito chat: the bind is the last moment to catch it, and it does."""
    core = harness.core
    conv = await core.store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    att = await core.attachments.add_file(
        data=png(400, 300), filename="late.png", mime="image/png", conversation_id=None
    )
    stored = await core.attachments.get(att.id)
    assert stored is not None and stored.path is not None and stored.path.exists()
    assert await core.attachments.thumbnail(att.id) is not None
    thumb_cache = stored.path.with_suffix(".thumb.jpg")
    assert thumb_cache.exists()

    harness.chat.push(FakeTurn(text="seen"))
    await core.engine.create_run(text="here", conversation_id=conv.id, attachment_ids=[att.id])
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")

    assert not stored.path.exists() and not thumb_cache.exists()
    after = await core.attachments.get(att.id)
    assert after is not None and after.private and after.path is None
    assert (await core.attachments.data_url(att.id) or "").startswith("data:image/")
    sent = harness.chat.calls[-1][0]
    user = [m for m in sent if m.role.value == "user" and not m.name][-1]
    assert user.attachments and user.attachments[0].data_url


async def test_start_up_scrubs_what_an_incognito_chat_left_on_the_disk(harness: Harness):
    """The state found on 2026-09-13: an incognito chat's clip sitting in data/attachments with
    a path and a transcript in the row. The next start removes the file, the thumbnail and the
    text, and keeps the row so the transcript still names the file. A plain chat's file stays."""
    core = harness.core
    conv = await core.store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    root = core.config.home / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    leaked = root / "att_old.mp4"
    leaked.write_bytes(b"not really a video")
    leaked.with_suffix(".thumb.jpg").write_bytes(b"not really a jpeg")
    await core.db.execute(
        "INSERT INTO attachments(id, conversation_id, message_id, kind, name, mime, bytes, path, text, meta, created_at)"
        " VALUES ('att_old', ?, NULL, 'video', '20260913_203501.mp4', 'video/mp4', 18, ?, 'what was said', '{}', ?)",
        (conv.id, str(leaked), datetime.now(UTC).isoformat()),
    )
    plain = await core.store.create_conversation()
    kept = await core.attachments.add_file(
        data=b"keep me", filename="keep.txt", mime="text/plain", conversation_id=plain.id
    )
    kept_stored = await core.attachments.get(kept.id)
    assert kept_stored is not None and kept_stored.path is not None

    assert await core.attachments.scrub_private() == 1
    assert not leaked.exists() and not leaked.with_suffix(".thumb.jpg").exists()
    row = await core.db.fetchone("SELECT path, text FROM attachments WHERE id = 'att_old'")
    assert row is not None and row["path"] is None and row["text"] is None
    assert kept_stored.path.exists()
    assert await core.attachments.scrub_private() == 0
    # The row is still there for the transcript, and the file is honestly gone.
    gone = await core.attachments.get("att_old")
    assert gone is not None and gone.path is None and await core.attachments.raw("att_old") is None


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        yield c


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_rest_serves_an_incognito_attachment_from_memory_and_forbids_caching(client: TestClient):
    r = client.post(
        "/api/attachments",
        files={"file": ("IMG_secret.png", png(300, 200), "image/png")},
        data={"incognito": "true"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    att_id = r.json()["id"]
    full = client.get(f"/api/attachments/{att_id}", headers=_h())
    assert full.status_code == 200 and full.headers["cache-control"] == "no-store"
    assert full.content[:4] == b"\x89PNG"
    thumb = client.get(f"/api/attachments/{att_id}?thumb=true", headers=_h())
    assert thumb.status_code == 200 and thumb.headers["cache-control"] == "no-store"
    assert thumb.headers["content-type"].startswith("image/jpeg")
    # A pasted note, the same way.
    r = client.post("/api/attachments/text", json={"text": "between us", "name": "n", "incognito": True}, headers=_h())
    assert r.status_code == 201
    t = client.get(f"/api/attachments/{r.json()['id']}/text", headers=_h())
    assert t.json()["text"] == "between us" and t.headers["cache-control"] == "no-store"

    # An ordinary upload keeps the day-long cache it always had.
    r = client.post("/api/attachments", files={"file": ("a.png", png(300, 200), "image/png")}, headers=_h())
    plain = client.get(f"/api/attachments/{r.json()['id']}", headers=_h())
    assert plain.headers["cache-control"] == "private, max-age=86400"
