"""Attachments: photos, files, pasted text and email threads, as one concept.

The chat has a single "+" and a single pipeline (docs/stories/09_attachments.md):

* an **image** is resized once on the way in (a phone photo is 4000 px wide and the model reads
  none of that) and reaches the model as an image part;
* everything else becomes **text** — extracted here for what the core can read on its own, or
  handed over already extracted (an Outlook thread, pasted text);
* the text goes into the per-turn context message, never into the stable system prefix, so the
  prompt cache survives (see engine/context.py).

Files live under ``JARVIS_HOME/attachments/<id>`` and die with their conversation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis_proto import Attachment, AttachmentKind, new_id

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024  # one upload; the phone chunks nothing below this
# A video is the one thing that arrives bigger than that as a matter of course — a minute of
# 1080p off a phone is 100 MB+ — and it is sampled down to a handful of frames anyway.
MAX_VIDEO_BYTES = 200 * 1024 * 1024
# Anthropic and vLLM both stop gaining accuracy above ~1568 px on the long edge, and a 4000 px
# photo costs several times the tokens for the same answer.
MAX_IMAGE_EDGE = 1568
THUMB_EDGE = 320
EXIF_ORIENTATION = 0x0112  # the tag a phone writes instead of rotating the pixels
IMAGE_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
VIDEO_MIME = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo", "video/3gpp"}
# What the model is shown of a clip. The vision tower reads frames, and every frame is roughly a
# picture's worth of tokens, so the sampling is the whole design: 1 frame a second, at most 32 of
# them, 768 px on the long edge. That is ~32 s of a clip watched evenly, for about the token cost
# of a handful of photos. Longer clips are not refused — they are sampled across their length, so
# a 5-minute video becomes 32 frames spread over 5 minutes.
VIDEO_FPS = 1.0
VIDEO_MAX_FRAMES = 32
VIDEO_EDGE = 768
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".log",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".sql",
    ".sh",
    ".ps1",
    ".html",
    ".css",
    ".xml",
    ".eml",
}
MAX_TEXT_CHARS = 60_000  # what one attachment may contribute before it is trimmed


class AttachmentError(ValueError):
    """Something the caller can fix: too big, unreadable, unknown."""


@dataclass(slots=True)
class Stored:
    attachment: Attachment
    path: Path | None


class AttachmentStore:
    def __init__(self, core: Core) -> None:
        self.core = core

    @property
    def root(self) -> Path:
        d = self.core.config.home / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- writing ---------------------------------------------------------------------------

    async def add_file(self, *, data: bytes, filename: str, mime: str, conversation_id: str | None) -> Attachment:
        if not data:
            raise AttachmentError("the file is empty")
        name = Path(filename or "attachment").name
        mime = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0].strip()
        is_video = mime in VIDEO_MIME or mime.startswith("video/")
        cap = MAX_VIDEO_BYTES if is_video else MAX_BYTES
        if len(data) > cap:
            raise AttachmentError(
                f"{filename} is {len(data) // 1024 // 1024} MB; the limit is {cap // 1024 // 1024} MB"
            )
        att_id = new_id("att")
        meta: dict[str, Any] = {}
        text: str | None = None

        if is_video:
            kind = AttachmentKind.VIDEO
            # Sampled down to the frames the model will actually be shown, once, here — so the
            # stored file IS what it watches and nothing re-decodes a 200 MB clip per turn.
            data, mime, meta = _prepare_video(data, mime, meta)
        elif mime in IMAGE_MIME or mime.startswith("image/"):
            kind = AttachmentKind.IMAGE
            # Resizing can re-encode (a 4000 px RGBA screenshot comes back as JPEG), so the mime
            # is whatever the BYTES are now — the name stays the one Arsen sent.
            data, mime, meta = _prepare_image(data, mime, meta)
        else:
            kind = AttachmentKind.DOCUMENT
            text, meta = extract_text(data, name, mime, meta)
            if text is None:
                raise AttachmentError(
                    f"{name}: no text could be read from a {mime} file. Put it in the shared folder "
                    "and ask Jarvis to open it with the file tools instead."
                )

        path = self.root / f"{att_id}{_suffix_for(name, mime)}"
        path.write_bytes(data)
        att = Attachment(id=att_id, kind=kind, name=name, mime=mime, bytes=len(data), text=text, meta=meta)
        await self._insert(att, conversation_id=conversation_id, path=path)
        return att

    async def add_text(
        self,
        *,
        text: str,
        name: str,
        kind: AttachmentKind = AttachmentKind.TEXT,
        conversation_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Attachment:
        """Pasted text, or a thread the host already turned into text: no file on disk."""
        body = text.strip()
        if not body:
            raise AttachmentError("nothing to attach")
        trimmed, meta_out = _trim(body, dict(meta or {}))
        att = Attachment(
            id=new_id("att"),
            kind=kind,
            name=name or "pasted text",
            mime="text/plain",
            bytes=len(body.encode()),
            text=trimmed,
            meta=meta_out,
        )
        await self._insert(att, conversation_id=conversation_id, path=None)
        return att

    async def _insert(self, att: Attachment, *, conversation_id: str | None, path: Path | None) -> None:
        await self.core.db.execute(
            "INSERT INTO attachments(id, conversation_id, message_id, kind, name, mime, bytes, path, text, meta,"
            " created_at) VALUES (?,?,NULL,?,?,?,?,?,?,?,?)",
            (
                att.id,
                conversation_id,
                att.kind.value,
                att.name,
                att.mime,
                att.bytes,
                str(path) if path else None,
                att.text,
                json.dumps(att.meta, ensure_ascii=False),
                att.created_at.isoformat(),
            ),
        )

    async def bind(self, ids: list[str], *, message_id: str, conversation_id: str) -> list[Attachment]:
        """Tie uploads to the message they were sent with (and to its conversation)."""
        out: list[Attachment] = []
        for att_id in ids:
            row = await self.core.db.fetchone("SELECT * FROM attachments WHERE id = ?", (att_id,))
            if row is None:
                continue
            await self.core.db.execute(
                "UPDATE attachments SET message_id = ?, conversation_id = ? WHERE id = ?",
                (message_id, conversation_id, att_id),
            )
            out.append(_attachment(row))
        return out

    # --- reading ---------------------------------------------------------------------------

    async def get(self, att_id: str) -> Stored | None:
        row = await self.core.db.fetchone("SELECT * FROM attachments WHERE id = ?", (att_id,))
        if row is None:
            return None
        return Stored(_attachment(row), Path(row["path"]) if row["path"] else None)

    async def for_message(self, message_id: str) -> list[Attachment]:
        rows = await self.core.db.fetchall(
            "SELECT * FROM attachments WHERE message_id = ? ORDER BY created_at", (message_id,)
        )
        return [_attachment(r) for r in rows]

    async def for_conversation(self, conversation_id: str) -> list[Attachment]:
        rows = await self.core.db.fetchall(
            "SELECT * FROM attachments WHERE conversation_id = ? ORDER BY created_at", (conversation_id,)
        )
        return [_attachment(r) for r in rows]

    async def for_messages(self, message_ids: list[str]) -> dict[str, list[Attachment]]:
        """One query for a whole transcript instead of one per message."""
        if not message_ids:
            return {}
        marks = ",".join("?" for _ in message_ids)
        rows = await self.core.db.fetchall(
            f"SELECT * FROM attachments WHERE message_id IN ({marks}) ORDER BY created_at", tuple(message_ids)
        )
        out: dict[str, list[Attachment]] = {}
        for row in rows:
            out.setdefault(row["message_id"], []).append(_attachment(row))
        return out

    async def delete(self, att_id: str) -> bool:
        stored = await self.get(att_id)
        if stored is None:
            return False
        if stored.path is not None:
            with contextlib.suppress(OSError):
                stored.path.unlink()
        await self.core.db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
        return True

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """Unlink every file a conversation's attachments own. The rows go with the conversation
        (ON DELETE CASCADE); the files on disk would not, and a chat that promised to leave
        nothing behind must not leave its photos in data/attachments."""
        rows = await self.core.db.fetchall(
            "SELECT path FROM attachments WHERE conversation_id = ? AND path IS NOT NULL", (conversation_id,)
        )
        for row in rows:
            with contextlib.suppress(OSError):
                Path(row["path"]).unlink()
        return len(rows)

    async def thumbnail(self, att_id: str) -> tuple[bytes, str] | None:
        """A small JPEG for the transcript: the picture, or a video's first frame. None for
        anything with nothing to show."""
        stored = await self.get(att_id)
        if stored is None or stored.path is None:
            return None
        kind = stored.attachment.kind
        if kind not in (AttachmentKind.IMAGE, AttachmentKind.VIDEO):
            return None
        cache = stored.path.with_suffix(".thumb.jpg")
        if cache.exists():
            return cache.read_bytes(), "image/jpeg"
        raw = stored.path.read_bytes()
        thumb = _video_poster(raw, THUMB_EDGE) if kind is AttachmentKind.VIDEO else _resize(raw, THUMB_EDGE, jpeg=True)
        if thumb is None:
            return None if kind is AttachmentKind.VIDEO else (raw, stored.attachment.mime)
        with contextlib.suppress(OSError):
            cache.write_bytes(thumb[0])
        return thumb[0], thumb[1]

    async def data_url(self, att_id: str) -> str | None:
        """The picture or the clip as a data: URL for a model request; None for anything the
        model is not shown."""
        import base64

        stored = await self.get(att_id)
        if stored is None or stored.path is None:
            return None
        if stored.attachment.kind not in (AttachmentKind.IMAGE, AttachmentKind.VIDEO):
            return None
        raw = stored.path.read_bytes()
        return f"data:{stored.attachment.mime};base64,{base64.b64encode(raw).decode('ascii')}"


# --- helpers ---------------------------------------------------------------------------------


def _attachment(row: Any) -> Attachment:
    return Attachment(
        id=row["id"],
        kind=AttachmentKind(row["kind"]),
        name=row["name"],
        mime=row["mime"],
        bytes=row["bytes"],
        text=row["text"],
        meta=json.loads(row["meta"]) if row["meta"] else {},
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(UTC),
    )


def _trim(text: str, meta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if len(text) <= MAX_TEXT_CHARS:
        return text, meta
    meta["truncated_chars"] = len(text) - MAX_TEXT_CHARS
    return text[:MAX_TEXT_CHARS] + f"\n\n[…{len(text) - MAX_TEXT_CHARS} more characters were not included]", meta


def _resize(data: bytes, edge: int, *, jpeg: bool = False) -> tuple[bytes, str] | None:
    """Down-scale to ``edge`` on the long side, as (bytes, mime).

    The mime comes back WITH the bytes because the two can disagree, and the caller must not
    guess: re-encoding used to be silent, so a resized screenshot reached the model as
    ``data:image/png;base64,<JPEG>`` — content a strict vision endpoint rejects outright.

    The source format is kept when Pillow can write it (a PNG stays a PNG; only JPEG needs the
    alpha flattened), so the common case neither changes mime nor pays JPEG's price on the flat
    areas a screenshot is made of. None when Pillow is missing, the file is not an image, or it
    is already inside ``edge``.

    Two things a phone photo needs and a screenshot does not. Its orientation lives in EXIF, and
    re-encoding drops EXIF — so a portrait photo reached the model lying on its side, which is
    most of the way to "I cannot read this". And the step from 4000 px to 1568 px is a big one:
    Pillow's default resample softens it, which reads as out-of-focus in an answer.
    """
    try:
        import io

        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            source_format = (opened.format or "PNG").upper()
            # Every orientation but "1" has to be baked into the pixels: a vision model decodes
            # the pixels and never reads the EXIF tag that says which way up they go.
            upright = opened.getexif().get(EXIF_ORIENTATION, 1) in (0, 1)
            img = ImageOps.exif_transpose(opened) or opened
            if max(img.size) <= edge and not jpeg and upright:
                return None
            # LANCZOS with a wide reducing_gap: the sharpest of Pillow's down-scalers, and the
            # difference is visible exactly where it matters — small text in a photo of a page.
            img.thumbnail((edge, edge), Image.Resampling.LANCZOS, reducing_gap=3.0)
            fmt = "JPEG" if jpeg else source_format
            try:
                return _encode(img, fmt)
            except Exception:  # a format Pillow reads but cannot write back (rare): fall to JPEG
                return _encode(img, "JPEG")
    except Exception as exc:
        log.warning("image resize failed: %s", exc)
        return None


def _encode(img: Any, fmt: str) -> tuple[bytes, str]:
    import io

    from PIL import Image

    out = io.BytesIO()
    if fmt in {"JPEG", "JPG"}:
        img.convert("RGB").save(out, format="JPEG", quality=85)  # JPEG cannot hold alpha
        return out.getvalue(), "image/jpeg"
    img.save(out, format=fmt)
    return out.getvalue(), Image.MIME.get(fmt) or "image/png"


def _prepare_image(data: bytes, mime: str, meta: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]]:
    """Size an image for the model. Pixels are the cost, not bytes.

    ``_resize`` only answers when the long edge is over the limit, so whatever it returns is a
    real down-scale and is always taken. Accepting it only when the file also got *smaller* (as
    this used to) kept a 4000 px screenshot at full resolution whenever its PNG happened to beat
    the resized encoding — which is most of them, and exactly the case the resize exists for.
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            meta["width"], meta["height"] = img.size
    except Exception:  # a file the decoder refuses is stored as-is and will fail loudly later
        return data, mime, meta
    smaller = _resize(data, MAX_IMAGE_EDGE)
    if smaller is None:
        return data, mime, meta
    meta["resized_from"] = f"{meta.get('width')}x{meta.get('height')}"
    meta["original_bytes"] = len(data)
    if smaller[1] != mime:
        meta["reencoded_from"] = mime
    return smaller[0], smaller[1], meta


def _prepare_video(data: bytes, mime: str, meta: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]]:
    """Re-sample a clip into the few frames the model is actually shown.

    The vision tower reads a video as frames and pays per frame, so what matters is not the file
    size but how many pictures come out of it. A phone clip is 30 fps: shown whole, ten seconds
    of it is 300 images and a context blown in one message. Here it becomes at most
    ``VIDEO_MAX_FRAMES`` frames at ``VIDEO_FPS``, spread evenly across the WHOLE clip when it is
    longer than that — so a five-minute video still arrives as 32 frames, one every ten seconds,
    rather than the first half-minute and nothing after it.

    Re-encoded to MJPEG in an MP4 container: every decoder in the chain (PyAV here, whatever vLLM
    uses there) reads it, the frames stay crisp because each one is a JPEG, and nothing depends
    on an H.264 encoder being compiled into the wheel — ``libx264`` is exactly what was missing
    on the machine this was first tried on.
    """
    import io

    try:
        import av
    except ImportError:  # pragma: no cover - av is a declared dependency
        return data, mime, meta
    try:
        with av.open(io.BytesIO(data), mode="r") as src:
            stream = next((s for s in src.streams.video), None)
            if stream is None:
                raise AttachmentError("no video track in this file")
            duration = float(src.duration / av.time_base) if src.duration else 0.0
            meta["duration_s"] = round(duration, 2)
            meta["source_size"] = f"{stream.width}x{stream.height}"
            meta["original_bytes"] = len(data)
            # One frame a second, but never more than the cap: a long clip is sampled across its
            # whole length instead of being cut off partway.
            wanted = VIDEO_MAX_FRAMES if duration * VIDEO_FPS > VIDEO_MAX_FRAMES else max(1, int(duration * VIDEO_FPS))
            step = duration / wanted if duration > 0 and wanted else 0.0
            frames: list[Any] = []
            next_at = 0.0
            for frame in src.decode(stream):
                at = float(frame.time or 0.0)
                if step and at + 1e-3 < next_at:
                    continue
                frames.append(frame.to_image())
                next_at = at + step if step else next_at
                if len(frames) >= wanted:
                    break
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError(f"this video could not be read ({exc.__class__.__name__}); try an MP4") from exc
    if not frames:
        raise AttachmentError("no frames could be read from this video")

    first = frames[0]
    scale = min(1.0, VIDEO_EDGE / max(first.width, first.height))
    width = max(2, int(first.width * scale)) & ~1  # even dimensions: encoders insist
    height = max(2, int(first.height * scale)) & ~1
    out = io.BytesIO()
    with av.open(out, mode="w", format="mp4") as dst:
        stream_out = dst.add_stream("mjpeg", rate=int(VIDEO_FPS) or 1)
        stream_out.width, stream_out.height = width, height
        stream_out.pix_fmt = "yuvj420p"
        for image in frames:
            picture = av.VideoFrame.from_image(image.resize((width, height)))
            for packet in stream_out.encode(picture):
                dst.mux(packet)
        for packet in stream_out.encode():
            dst.mux(packet)
    meta["frames"] = len(frames)
    meta["fps"] = VIDEO_FPS
    meta["width"], meta["height"] = width, height
    meta["sampled"] = f"{len(frames)} frames over {meta['duration_s']}s"
    return out.getvalue(), "video/mp4", meta


def _video_poster(data: bytes, edge: int) -> tuple[bytes, str] | None:
    """The first frame of a clip as a small JPEG, so a video row in the transcript shows the
    thing itself rather than a grey box."""
    import io

    try:
        import av
    except ImportError:  # pragma: no cover - av is a declared dependency
        return None
    try:
        with av.open(io.BytesIO(data), mode="r") as src:
            stream = next((s for s in src.streams.video), None)
            if stream is None:
                return None
            for frame in src.decode(stream):
                image = frame.to_image()
                image.thumbnail((edge, edge))
                out = io.BytesIO()
                image.convert("RGB").save(out, format="JPEG", quality=85)
                return out.getvalue(), "image/jpeg"
    except Exception as exc:
        log.warning("video poster failed: %s", exc)
    return None


def _suffix_for(name: str, mime: str) -> str:
    """The stored file's suffix, taken from the mime so the bytes and the extension agree."""
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    return Path(name).suffix.lower()


def extract_text(data: bytes, name: str, mime: str, meta: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Text out of a file the core can read on its own. None means "not something we can read"."""
    suffix = Path(name).suffix.lower()
    if mime == "application/pdf" or suffix == ".pdf":
        return _pdf_text(data, meta)
    if mime.startswith("text/") or suffix in TEXT_SUFFIXES or mime in {"application/json", "application/xml"}:
        text = data.decode("utf-8", errors="replace")
        trimmed, meta = _trim(text, meta)
        return trimmed, meta
    return None, meta


def _pdf_text(data: bytes, meta: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    try:
        import io

        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a declared dependency
        return None, meta
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        log.warning("pdf text extraction failed: %s", exc)
        return None, meta
    meta["pages"] = len(pages)
    text = "\n\n".join(f"[page {i}]\n{p.strip()}" for i, p in enumerate(pages, 1) if p.strip())
    if not text.strip():
        # A scanned PDF: say so rather than attaching an empty document.
        meta["scanned"] = True
        return "[This PDF has no text layer — it looks scanned. Nothing could be read from it.]", meta
    return _trim(text, meta)
