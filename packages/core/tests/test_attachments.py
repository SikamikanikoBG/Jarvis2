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


def rgba_png(width: int, height: int) -> bytes:
    """A PNG with an alpha channel — what a Windows screenshot actually is."""
    from PIL import Image

    img = Image.new("RGBA", (width, height))
    img.putdata([((x * 7) % 256, (x * y) % 256, (y * 3) % 256, 255) for y in range(height) for x in range(width)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def flat_rgba_png(width: int, height: int) -> bytes:
    """A big single-colour RGBA PNG: the shape whose resized JPEG is SMALLER than the original,
    which is what used to make the pipeline swap the bytes for JPEG and keep saying png."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


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


def mime_of(data: bytes) -> str:
    """What the bytes actually are, from their magic number."""
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:3] == b"GIF":
        return "image/gif"
    return f"unknown:{data[:8]!r}"


async def test_a_stored_image_is_what_its_mime_says_it_is(harness: Harness):
    """The recorded mime, the file on disk and the data: URL must all agree with the BYTES.

    Resizing re-encodes, and the mime used to be left at whatever the upload claimed: a resized
    RGBA screenshot was stored and sent as ``data:image/png;base64,<JPEG>``, which a strict
    vision endpoint rejects outright. The invariant is checked over the sizes that take each
    branch — under the limit (untouched) and over it (re-encoded).
    """
    core = harness.core
    cases = [
        ("Screenshot.png", "image/png", rgba_png(2000, 1200)),  # over the limit, has alpha
        ("tiny.png", "image/png", rgba_png(64, 48)),  # under the limit, untouched
        ("photo.png", "image/png", png(2400, 1400)),  # over the limit, no alpha
        ("flat.png", "image/png", flat_rgba_png(3000, 2000)),  # the one that used to become JPEG
    ]
    for filename, mime, data in cases:
        att = await core.attachments.add_file(data=data, filename=filename, mime=mime, conversation_id=None)
        stored = await core.attachments.get(att.id)
        assert stored is not None and stored.path is not None, filename
        on_disk = stored.path.read_bytes()
        assert att.name == filename  # the name Arsen sent is still his
        assert att.mime == mime_of(on_disk), f"{filename}: mime {att.mime} over {mime_of(on_disk)} bytes"
        assert att.bytes == len(on_disk)
        assert stored.path.suffix == {"image/png": ".png", "image/jpeg": ".jpg"}[att.mime]
        url = await core.attachments.data_url(att.id) or ""
        assert url.startswith(f"data:{mime_of(on_disk)};base64,")
        # A thumbnail is always a JPEG and always says so.
        thumb = await core.attachments.thumbnail(att.id)
        assert thumb is not None and thumb[1] == "image/jpeg" == mime_of(thumb[0])


async def test_an_oversized_image_is_scaled_down_even_when_that_makes_the_file_bigger(harness: Harness):
    """Pixels are what the model pays for, not bytes.

    A screenshot is flat colour, so its PNG beats any re-encoding of the smaller image. Taking
    the resize only when the file also shrank left those at full resolution — precisely the
    4000 px images the resize exists for.
    """
    core = harness.core
    shot = rgba_png(2400, 1600)
    att = await core.attachments.add_file(data=shot, filename="wide.png", mime="image/png", conversation_id=None)
    assert att.meta["resized_from"] == "2400x1600" and att.meta["original_bytes"] == len(shot)
    stored = await core.attachments.get(att.id)
    assert stored is not None and stored.path is not None
    from PIL import Image

    with Image.open(stored.path) as img:
        assert max(img.size) == MAX_IMAGE_EDGE

    # Under the limit nothing is touched at all — same bytes, no resize note.
    small = rgba_png(64, 48)
    kept = await core.attachments.add_file(data=small, filename="tiny2.png", mime="image/png", conversation_id=None)
    kept_stored = await core.attachments.get(kept.id)
    assert kept_stored is not None and kept_stored.path is not None
    assert kept_stored.path.read_bytes() == small and "resized_from" not in kept.meta


async def test_a_portrait_photo_reaches_the_model_the_right_way_up(harness: Harness):
    """A phone does not rotate the pixels, it writes EXIF Orientation and leaves them sideways.

    Re-encoding drops EXIF, and a vision model reads pixels, not tags — so a photo held upright
    arrived lying on its side, one of the reasons the answer kept being that it could not be
    read. Now the rotation is baked in, whatever the size: even an image already inside the
    limit is re-encoded when its orientation is not 1.
    """
    from PIL import Image

    def portrait(width: int, height: int, orientation: int) -> bytes:
        buf = io.BytesIO()
        img = Image.new("RGB", (width, height), (30, 90, 200))
        exif = img.getexif()
        exif[0x0112] = orientation
        img.save(buf, format="JPEG", exif=exif)
        return buf.getvalue()

    core = harness.core
    # 6 = "rotate 90° clockwise to view": stored landscape, meant to be seen as portrait.
    shot = portrait(2400, 1600, 6)
    att = await core.attachments.add_file(data=shot, filename="IMG_9001.jpg", mime="image/jpeg", conversation_id=None)
    stored = await core.attachments.get(att.id)
    assert stored is not None and stored.path is not None
    with Image.open(stored.path) as img:
        assert img.height > img.width, "the photo is stored upright, not on its side"
        assert max(img.size) == MAX_IMAGE_EDGE

    # Small enough to skip the resize, but still not upright: it must be turned all the same.
    small = portrait(300, 200, 6)
    turned = await core.attachments.add_file(data=small, filename="small.jpg", mime="image/jpeg", conversation_id=None)
    turned_stored = await core.attachments.get(turned.id)
    assert turned_stored is not None and turned_stored.path is not None
    with Image.open(turned_stored.path) as img:
        assert img.size == (200, 300)


def clip(seconds: float, fps: int, width: int = 640, height: int = 480, *, audio: bool = False) -> bytes:
    """A real encoded video, made here so the test needs no ffmpeg binary and no fixture file.
    With ``audio`` it carries a 440 Hz tone: an audio TRACK, which is what the extraction needs."""
    import av
    from PIL import Image

    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mp4") as dst:
        stream = dst.add_stream("mpeg4", rate=fps)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        sound = dst.add_stream("aac", rate=44100, layout="mono") if audio else None
        for i in range(int(seconds * fps)):
            shade = (i * 7) % 256
            frame = av.VideoFrame.from_image(Image.new("RGB", (width, height), (shade, 40, 200 - shade)))
            for packet in stream.encode(frame):
                dst.mux(packet)
        if sound is not None:
            import array
            import math

            # 1024-sample frames of a 440 Hz tone, built without numpy (not a core dependency).
            per_frame = 1024
            for n in range(int(seconds * 44100) // per_frame):
                pcm = array.array(
                    "h",
                    (int(9000 * math.sin(2 * math.pi * 440 * (n * per_frame + i) / 44100)) for i in range(per_frame)),
                )
                aframe = av.AudioFrame(format="s16", layout="mono", samples=per_frame)
                aframe.planes[0].update(pcm.tobytes())
                aframe.sample_rate = 44100
                aframe.pts = n * per_frame
                for packet in sound.encode(aframe):
                    dst.mux(packet)
            for packet in sound.encode():
                dst.mux(packet)
        for packet in stream.encode():
            dst.mux(packet)
    return buf.getvalue()


async def test_a_video_is_sampled_to_frames_and_reaches_the_model_as_a_video_part(harness: Harness):
    """A clip is frames to the model, and frames are what it pays for.

    Ten seconds at 30 fps is 300 pictures; shown whole it is a context blown by one message. The
    clip is re-sampled once on the way in — a frame a second, 32 at most — and the stored file is
    what the model watches. Verified against the qwen3.8 endpoint on 2026-09-13: an mp4 data URI
    in a `video_url` part, answered correctly.
    """
    core = harness.core
    att = await core.attachments.add_file(
        data=clip(6, 30), filename="VID_20260913.mp4", mime="video/mp4", conversation_id=None
    )
    assert att.kind is AttachmentKind.VIDEO
    assert att.meta["frames"] == 6 and att.meta["duration_s"] == pytest.approx(6.0, abs=0.5)
    assert att.bytes < att.meta["original_bytes"], "the stored clip is the sampled one"
    assert att.mime == "video/mp4"

    # A long clip is spread across its whole length rather than cut off after 32 seconds.
    long_att = await core.attachments.add_file(
        data=clip(60, 15), filename="long.mp4", mime="video/mp4", conversation_id=None
    )
    assert long_att.meta["frames"] == 32
    assert "over 60" in long_att.meta["sampled"]

    # No audio track: the message says so, rather than the model guessing what was said.
    assert att.text is None and att.meta["audio"] == "none"

    # The transcript gets a poster frame, not a grey box.
    poster = await core.attachments.thumbnail(att.id)
    assert poster is not None and poster[1] == "image/jpeg" and len(poster[0]) > 0

    # And it reaches the model as a video part, next to the text.
    harness.chat.push(FakeTurn(text="A colour fade."))
    conv = await core.store.create_conversation()
    await core.engine.create_run(text="what happens here?", conversation_id=conv.id, attachment_ids=[att.id])
    sub = harness.subscribe(conv.id)
    await harness.wait_for(sub, "run.done")
    sent = harness.chat.calls[-1][0]
    parts = [p for m in sent if isinstance(m.attachments, list) for p in m.attachments]
    assert any(
        p.kind is AttachmentKind.VIDEO and (p.data_url or "").startswith("data:video/mp4;base64,") for p in parts
    )

    from jarvis_core.models.openai_compat import to_openai_messages

    payload = to_openai_messages(sent)
    video_parts = [
        part
        for m in payload
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") == "video_url"
    ]
    assert video_parts, "the adapter sends a video_url part, which is what vLLM takes"
    assert video_parts[0]["video_url"]["url"].startswith("data:video/mp4;base64,")


async def test_what_is_said_in_a_video_reaches_the_model_as_text(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    """ "Е не успя ли да чуеш какво ти казвам във видеото." The frames carry no sound and a vision
    model cannot read lips. The clip's audio goes through the same Whisper the microphone uses,
    and the words land in the message next to the frames — or, when Whisper is down, the message
    says the sound was NOT heard instead of letting the model pretend."""
    from jarvis_core.engine.context import _with_attachment_note
    from jarvis_core.features.stt import SttError, SttResult
    from jarvis_proto import Message, Role

    core = harness.core
    heard: list[tuple[str, str, int]] = []

    async def fake_transcribe(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        heard.append((filename, mime, len(audio)))
        return SttResult(text="колко време е, сърбеж, отделяне", language="bg", backend="fake", duration_ms=5)

    monkeypatch.setattr(core.transcriber, "transcribe", fake_transcribe)
    att = await core.attachments.add_file(
        data=clip(2, 10, audio=True), filename="v.mp4", mime="video/mp4", conversation_id=None
    )
    assert heard and heard[0][0] == "clip.wav" and heard[0][1] == "audio/wav" and heard[0][2] > 44
    assert (
        att.text == "колко време е, сърбеж, отделяне"
        and att.meta["audio"] == "transcribed"
        and att.meta["language"] == "bg"
    )
    note = _with_attachment_note(
        Message(role=Role.USER, content="виж"), [att.model_copy(update={"data_url": "data:video/mp4;base64,x"})]
    )
    assert "what is said in it (bg):]" in note and note.endswith("колко време е, сърбеж, отделяне")

    async def whisper_down(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        raise SttError("STT backend unreachable: ConnectError")

    monkeypatch.setattr(core.transcriber, "transcribe", whisper_down)
    deaf = await core.attachments.add_file(
        data=clip(2, 10, audio=True), filename="v2.mp4", mime="video/mp4", conversation_id=None
    )
    assert deaf.text is None and deaf.meta["audio"].startswith("not transcribed")
    note = _with_attachment_note(
        Message(role=Role.USER, content="виж"), [deaf.model_copy(update={"data_url": "data:video/mp4;base64,x"})]
    )
    assert "its sound was NOT heard" in note


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
