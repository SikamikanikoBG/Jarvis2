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
# Anthropic and vLLM both stop gaining accuracy above ~1568 px on the long edge, and a 4000 px
# photo costs several times the tokens for the same answer.
MAX_IMAGE_EDGE = 1568
THUMB_EDGE = 320
IMAGE_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
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
        if len(data) > MAX_BYTES:
            raise AttachmentError(
                f"{filename} is {len(data) // 1024 // 1024} MB; the limit is {MAX_BYTES // 1024 // 1024} MB"
            )
        name = Path(filename or "attachment").name
        mime = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0].strip()
        att_id = new_id("att")
        meta: dict[str, Any] = {}
        text: str | None = None

        if mime in IMAGE_MIME or mime.startswith("image/"):
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

    async def thumbnail(self, att_id: str) -> tuple[bytes, str] | None:
        """A small JPEG for the transcript; None when the attachment is not an image."""
        stored = await self.get(att_id)
        if stored is None or stored.attachment.kind is not AttachmentKind.IMAGE or stored.path is None:
            return None
        cache = stored.path.with_suffix(".thumb.jpg")
        if cache.exists():
            return cache.read_bytes(), "image/jpeg"
        thumb = _resize(stored.path.read_bytes(), THUMB_EDGE, jpeg=True)
        if thumb is None:
            return stored.path.read_bytes(), stored.attachment.mime
        with contextlib.suppress(OSError):
            cache.write_bytes(thumb[0])
        return thumb[0], thumb[1]

    async def data_url(self, att_id: str) -> str | None:
        """The image as a data: URL for a model request, or None if it is not an image."""
        import base64

        stored = await self.get(att_id)
        if stored is None or stored.attachment.kind is not AttachmentKind.IMAGE or stored.path is None:
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
    """
    try:
        import io

        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if max(img.size) <= edge and not jpeg:
                return None
            img.thumbnail((edge, edge))
            fmt = "JPEG" if jpeg else (img.format or "PNG").upper()
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
