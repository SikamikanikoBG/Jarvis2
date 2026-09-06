"""Attachments: photos, documents, pasted text — one concept, one pipeline (story 09)."""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from jarvis_core.features.attachments import MAX_IMAGE_EDGE, AttachmentError
from jarvis_core.models.fake import FakeTurn
from jarvis_proto import AttachmentKind, Message
from tests.conftest import Harness


def png(width: int, height: int) -> bytes:
    """A real PNG of the given size (Pillow reads it; no fixture files needed)."""
    raw = b"".join(b"\x00" + bytes([(x * 7) % 256, (y * 5) % 256, 128] * width) for y in range(height) for x in [0])
    raw = b"".join(b"\x00" + bytes([(x + y) % 256, 60, 200] * 1) * width for y in range(height) for x in [0])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def pdf(text: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def test_a_phone_photo_is_resized_and_reaches_the_model_as_an_image(harness: Harness):
    core = harness.core
    big = png(2400, 1400)  # what a phone camera hands over
    att = await core.attachments.add_file(data=big, filename="IMG_4821.png", mime="image/png", conversation_id=None)
    assert att.kind is AttachmentKind.IMAGE
    assert att.meta["width"] == 2400 and att.meta["resized_from"] == "2400x1400"
    assert att.bytes < att.meta["original_bytes"]
    stored = await core.attachments.get(att.id)
    assert stored is not None and stored.path is not None
    from PIL import Image

    with Image.open(stored.path) as img:
        assert max(img.size) <= MAX_IMAGE_EDGE

    # Sent with a message, it reaches the model as an image part, not as a filename.
    harness.chat.push(FakeTurn(text="A red square."))
    conv = await core.store.create_conversation()
    await core.engine.create_run(text="what is this?", conversation_id=conv.id, attachment_ids=[att.id])
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    sent = harness.chat.calls[-1][0]
    user = [m for m in sent if m.role.value == "user" and not m.name][-1]
    assert user.attachments and user.attachments[0].data_url and user.attachments[0].data_url.startswith("data:image/")
    assert "[image attached: IMG_4821.png" in user.content

    from jarvis_core.models.openai_compat import to_openai_messages

    payload = to_openai_messages([user])[0]
    assert isinstance(payload["content"], list)
    assert [p["type"] for p in payload["content"]] == ["text", "image_url"]

    from jarvis_core.models.ollama import to_ollama_messages

    assert to_ollama_messages([user])[0]["images"][0].startswith("iVBOR")  # bare base64, no data: prefix


async def test_a_document_becomes_text_the_model_can_read(harness: Harness):
    core = harness.core
    att = await core.attachments.add_file(
        data=b"line one\nline two", filename="notes.md", mime="text/markdown", conversation_id=None
    )
    assert att.kind is AttachmentKind.DOCUMENT and att.text == "line one\nline two"

    harness.chat.push(FakeTurn(text="ok"))
    conv = await core.store.create_conversation()
    await core.engine.create_run(text="summarise", conversation_id=conv.id, attachment_ids=[att.id])
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    user = [m for m in harness.chat.calls[-1][0] if m.role.value == "user" and not m.name][-1]
    assert "[attached notes.md" in user.content and "line two" in user.content
    assert not any(a.data_url for a in user.attachments)  # nothing to show, only to read


async def test_a_scanned_pdf_says_it_has_no_text_instead_of_attaching_nothing(harness: Harness):
    att = await harness.core.attachments.add_file(
        data=pdf("ignored"), filename="scan.pdf", mime="application/pdf", conversation_id=None
    )
    assert att.meta["scanned"] is True and "no text layer" in (att.text or "")


async def test_refusals_are_specific(harness: Harness):
    core = harness.core
    with pytest.raises(AttachmentError, match="empty"):
        await core.attachments.add_file(data=b"", filename="x.txt", mime="text/plain", conversation_id=None)
    with pytest.raises(AttachmentError, match="the limit is"):
        await core.attachments.add_file(
            data=b"x" * (26 * 1024 * 1024), filename="big.txt", mime="text/plain", conversation_id=None
        )
    with pytest.raises(AttachmentError, match="no text could be read"):
        await core.attachments.add_file(
            data=b"\x00\x01\x02binary", filename="thing.bin", mime="application/octet-stream", conversation_id=None
        )
    with pytest.raises(AttachmentError, match="nothing to attach"):
        await core.attachments.add_text(text="   ", name="empty")


async def test_pasted_text_and_deletion(harness: Harness):
    core = harness.core
    att = await core.attachments.add_text(text="a stack trace\nline 2", name="paste.txt")
    assert att.kind is AttachmentKind.TEXT and att.text is not None and "stack trace" in att.text
    stored = await core.attachments.get(att.id)
    assert stored is not None and stored.path is None  # text needs no file
    assert await core.attachments.delete(att.id) is True
    assert await core.attachments.get(att.id) is None
    assert await core.attachments.delete(att.id) is False


async def test_attachments_follow_the_message_in_the_transcript(harness: Harness):
    core = harness.core
    conv = await core.store.create_conversation()
    att = await core.attachments.add_text(text="hello", name="note.txt", conversation_id=conv.id)
    msg = await core.store.add_message(Message.user("see this", conversation_id=conv.id))
    bound = await core.attachments.bind([att.id, "att_missing"], message_id=msg.id or "", conversation_id=conv.id)
    assert [a.id for a in bound] == [att.id]  # an unknown id is skipped, not an error
    assert [a.id for a in await core.attachments.for_message(msg.id or "")] == [att.id]
    assert [a.id for a in await core.attachments.for_conversation(conv.id)] == [att.id]
