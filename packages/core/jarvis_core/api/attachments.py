"""Upload, fetch and delete attachments (docs/stories/09_attachments.md)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from jarvis_core.api.deps import core_of, require_token
from jarvis_core.features.attachments import AttachmentError
from jarvis_proto import Attachment, AttachmentKind

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class TextAttachment(BaseModel):
    text: str
    name: str = "pasted text"
    conversation_id: str | None = None
    #: The chat this is going into will be incognito but does not exist yet (first message).
    incognito: bool = False


# An incognito attachment must not be kept by any cache between the core and the eye that reads
# it - the phone browser's disk cache is a place, and "nothing stored" includes it.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_A_DAY = {"Cache-Control": "private, max-age=86400"}


@router.post("/attachments", response_model=Attachment, status_code=201)
async def upload(
    request: Request,
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    incognito: bool = Form(default=False),
) -> Attachment:
    """One file: a photo, a clip, a document, anything. Images are resized, videos are sampled
    down to the frames the model is shown, documents are read to text. ``incognito`` says the
    chat this goes into is private but not created yet (the first message uploads before
    ``run.create``); for an existing chat the conversation's own flag is what counts."""
    core = core_of(request)
    try:
        return await core.attachments.add_file(
            data=await file.read(),
            filename=file.filename or "attachment",
            mime=file.content_type or "",
            conversation_id=conversation_id,
            incognito=incognito,
        )
    except AttachmentError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/attachments/text", response_model=Attachment, status_code=201)
async def upload_text(request: Request, body: TextAttachment) -> Attachment:
    """Pasted text as an attachment, so a wall of log output does not become the message itself."""
    try:
        return await core_of(request).attachments.add_text(
            text=body.text, name=body.name, conversation_id=body.conversation_id, incognito=body.incognito
        )
    except AttachmentError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/attachments/{attachment_id}")
async def download(request: Request, attachment_id: str, thumb: bool = False) -> Response:
    core = core_of(request)
    stored = await core.attachments.get(attachment_id)
    if stored is None:
        raise HTTPException(404, "attachment not found")
    caching = _NO_STORE if stored.private else _A_DAY
    if thumb:
        made = await core.attachments.thumbnail(attachment_id)
        if made is not None:
            data, mime = made
            return Response(content=data, media_type=mime, headers=caching)
    if stored.private:
        # Memory only: served from there, and told to stay nowhere.
        got = await core.attachments.raw(attachment_id)
        if got is not None:
            data, mime = got
            return Response(
                content=data,
                media_type=mime,
                headers={**caching, "Content-Disposition": _inline(stored.attachment.name)},
            )
    if stored.path is None or not stored.path.exists():
        if stored.attachment.text is not None:
            return Response(content=stored.attachment.text, media_type="text/plain; charset=utf-8", headers=caching)
        raise HTTPException(410, "the file is no longer stored")
    return FileResponse(
        stored.path,
        media_type=stored.attachment.mime,
        filename=stored.attachment.name,
        headers=caching,
    )


def _inline(name: str) -> str:
    from urllib.parse import quote

    return f"inline; filename*=UTF-8''{quote(name)}"


@router.get("/attachments/{attachment_id}/text")
async def read_text(request: Request, attachment_id: str) -> Response:
    """What the model will see of a non-image attachment."""
    stored = await core_of(request).attachments.get(attachment_id)
    if stored is None:
        raise HTTPException(404, "attachment not found")
    att = stored.attachment
    body = {"id": att.id, "name": att.name, "kind": att.kind.value, "text": att.text or "", "meta": att.meta}
    return JSONResponse(body, headers=_NO_STORE if stored.private else {})


@router.delete("/attachments/{attachment_id}", status_code=204)
async def remove(request: Request, attachment_id: str) -> Response:
    if not await core_of(request).attachments.delete(attachment_id):
        raise HTTPException(404, "attachment not found")
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/attachments", response_model=list[Attachment])
async def by_conversation(request: Request, conversation_id: str) -> list[Attachment]:
    return await core_of(request).attachments.for_conversation(conversation_id)


__all__ = ["AttachmentKind", "router"]
