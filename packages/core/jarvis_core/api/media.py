"""STT upload, meetings, triage, RSVP and shadow endpoints (docs/API.md)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jarvis_core.api.deps import core_of, require_token
from jarvis_core.features.mail import Mailbox, MailError
from jarvis_core.features.maildesk import MailDeskError
from jarvis_core.features.stt import SttError
from jarvis_core.features.tts import TtsError
from jarvis_proto import Conversation, MailAccount, Meeting, MeetingDetail, TriageState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.post("/stt")
async def stt(
    request: Request, audio: UploadFile = File(...), language: str | None = Form(default=None)
) -> dict[str, Any]:
    core = core_of(request)
    data = await audio.read()
    if not data:
        raise HTTPException(422, "empty audio")
    try:
        result = await core.transcriber.transcribe(
            data,
            filename=audio.filename or "audio.webm",
            mime=audio.content_type or "audio/webm",
            language=language or None,
        )
    except SttError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "text": result.text,
        "language": result.language,
        "backend": result.backend,
        "duration_ms": result.duration_ms,
        "warning": result.warning,
    }


class TtsRequest(BaseModel):
    text: str
    lang: str = "bg"
    #: False for an incognito call: the sentence is synthesised but never written to the disk cache.
    cache: bool = True


@router.post("/tts")
async def tts(request: Request, body: TtsRequest) -> Response:
    """One sentence as MP3, in the neural voice configured for its language. 502 when the voice
    service cannot be reached - the browser then says the sentence with the device's own voice."""
    core = core_of(request)
    try:
        audio, mime = await core.synthesizer.synthesize(body.text, body.lang, cache=body.cache)
    except TtsError as exc:
        raise HTTPException(502, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "the voice service took too long") from exc
    caching = {"Cache-Control": "private, max-age=86400"} if body.cache else {"Cache-Control": "no-store"}
    return Response(content=audio, media_type=mime, headers=caching)


# --- meetings -------------------------------------------------------------------------------


class MeetingStart(BaseModel):
    title: str | None = None
    host: str


@router.get("/meetings", response_model=list[Meeting])
async def list_meetings(request: Request) -> list[Meeting]:
    return await core_of(request).meetings.list()


@router.post("/meetings", response_model=Meeting, status_code=201)
async def start_meeting(request: Request, body: MeetingStart) -> Meeting:
    try:
        return await core_of(request).meetings.start(title=body.title, host=body.host)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/meetings/{meeting_id}", response_model=MeetingDetail)
async def get_meeting(request: Request, meeting_id: str) -> MeetingDetail:
    detail = await core_of(request).meetings.detail(meeting_id)
    if detail is None:
        raise HTTPException(404, "meeting not found")
    return detail


@router.delete("/meetings/{meeting_id}", status_code=204)
async def delete_meeting(request: Request, meeting_id: str, keep_conversation: bool = False) -> Response:
    """Delete a recording: its rows, its frames on disk and, unless asked otherwise, the
    conversation holding its transcript. A recording still running is stopped first."""
    ok = await core_of(request).meetings.delete(meeting_id, with_conversation=not keep_conversation)
    if not ok:
        raise HTTPException(404, "meeting not found")
    return Response(status_code=204)


@router.post("/meetings/{meeting_id}/stop", response_model=Meeting)
async def stop_meeting(request: Request, meeting_id: str) -> Meeting:
    try:
        return await core_of(request).meetings.stop(meeting_id)
    except KeyError as exc:
        raise HTTPException(404, "meeting not found") from exc


@router.post("/meetings/{meeting_id}/chunks")
async def meeting_chunk(
    request: Request, meeting_id: str, audio: UploadFile = File(...), t0: float = Form(default=0.0)
) -> dict[str, Any]:
    core = core_of(request)
    data = await audio.read()
    try:
        added = await core.meetings.ingest_chunk(
            meeting_id, data, t0, filename=audio.filename or "chunk.wav", mime=audio.content_type or "audio/wav"
        )
    except KeyError as exc:
        raise HTTPException(404, "meeting not found") from exc
    except SttError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"segments_added": added}


@router.post("/meetings/{meeting_id}/frames")
async def meeting_frame(
    request: Request,
    meeting_id: str,
    image: UploadFile = File(...),
    at: float = Form(default=0.0),
    ocr: str | None = Form(default=None),
) -> dict[str, Any]:
    try:
        seq = await core_of(request).meetings.add_frame(meeting_id, await image.read(), at, ocr=ocr)
    except KeyError as exc:
        raise HTTPException(404, "meeting not found") from exc
    return {"seq": seq, "url": f"/api/meetings/{meeting_id}/frames/{seq}"}


@router.get("/meetings/{meeting_id}/frames/{seq}")
async def meeting_frame_file(request: Request, meeting_id: str, seq: int) -> FileResponse:
    path = await core_of(request).meetings.frame_path(meeting_id, seq)
    if path is None or not path.exists():
        raise HTTPException(404, "frame not found")
    return FileResponse(path, media_type="image/png")


# --- mail accounts (IMAP) -------------------------------------------------------------------


@router.post("/mail/test")
async def mail_test(request: Request, account: MailAccount) -> dict[str, Any]:
    """Log in with these (unsaved) details and count the Inbox - the Settings "Test" button.
    An empty app_password means "the one already saved for this address"."""
    if not account.app_password:
        saved = next(
            (a for a in core_of(request).settings.mail_accounts if a.address.lower() == account.address.lower()), None
        )
        if saved is None:
            return {"ok": False, "error": "no app password entered"}
        account = account.model_copy(update={"app_password": saved.app_password})
    try:
        return await asyncio.to_thread(Mailbox(account).check)
    except (MailError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:300]}


# --- triage ---------------------------------------------------------------------------------


@router.get("/triage/state", response_model=list[TriageState])
async def triage_state(request: Request) -> list[TriageState]:
    return await core_of(request).triage.states()


@router.post("/triage/run")
async def triage_run(
    request: Request,
    dry_run: bool = False,
    folder: str | None = None,
    limit: int | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Run one triage pass now. ``?dry_run=true`` classifies and reports the moves it would
    make without moving, recording or advancing anything; add ``folder=`` (dry run only) to
    sample an already-sorted folder and compare the proposal with where the mail lives, and
    ``account=`` to work on one mailbox (folders differ between the work one and a personal one)."""
    if folder and not dry_run:
        raise HTTPException(422, "folder sampling is only allowed with dry_run=true")
    report = await core_of(request).triage.run_once(dry_run=dry_run, folder=folder, limit=limit, account=account)
    return {
        "run_id": None,
        "dry_run": report.dry_run,
        "processed": report.processed,
        "routed": report.routed,
        "accounts": report.accounts,
        "errors": report.errors,
        "proposed": report.proposed,
        "alerts": report.alerts,
    }


@router.post("/triage/audit")
async def triage_audit(
    request: Request, hours: int = 24, sample: int | None = None, post: bool = False
) -> dict[str, Any]:
    """Audit the live triage decisions of the last ``hours`` now (the daily one runs at
    ``settings.triage.audit_hour``): mix per folder, rule violations, judge agreement on a
    sample and each disagreement. ``post=true`` also writes it to the Triage audit conversation
    and pushes the one-line summary. Reads only; never moves mail."""
    audit = core_of(request).triage_audit
    report = await audit.run(hours=max(1, min(hours, 24 * 14)), sample=sample, post=post)
    return {**report, "text": audit.render(report)}


# --- the mail desk ----------------------------------------------------------------------------


class MarkReadRequest(BaseModel):
    entry_ids: list[str]
    read: bool = True
    account: str | None = None


class MailSessionRequest(BaseModel):
    entry_id: str
    account: str | None = None


def _desk_error(exc: MailDeskError) -> HTTPException:
    # 502: the core is fine, the mailbox behind it is not - the screen shows the reason as is.
    return HTTPException(502, str(exc))


@router.get("/mail/threads")
async def mail_threads(request: Request, account: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """Unread mail of the inbox grouped into threads, newest first (cached for a minute;
    ``refresh=true`` reads the mailbox again)."""
    try:
        return await core_of(request).maildesk.threads(account, refresh=refresh)
    except MailDeskError as exc:
        raise _desk_error(exc) from exc


@router.get("/mail/thread")
async def mail_thread(request: Request, entry_id: str, account: str | None = None) -> dict[str, Any]:
    """Every message of the conversation ``entry_id`` belongs to, oldest first, Sent Items too."""
    try:
        return await core_of(request).maildesk.thread(entry_id, account)
    except MailDeskError as exc:
        raise _desk_error(exc) from exc


@router.get("/mail/message")
async def mail_message(request: Request, entry_id: str, account: str | None = None) -> dict[str, Any]:
    """One message with its whole body (a thread shows previews of up to 4,000 characters)."""
    try:
        return await core_of(request).maildesk.message(entry_id, account)
    except MailDeskError as exc:
        raise _desk_error(exc) from exc


@router.post("/mail/read")
async def mail_mark_read(request: Request, body: MarkReadRequest) -> dict[str, Any]:
    """Mark messages read (or unread); answers which changed and which did not."""
    try:
        return await core_of(request).maildesk.mark_read(body.entry_ids, read=body.read, account=body.account)
    except MailDeskError as exc:
        raise _desk_error(exc) from exc


@router.post("/mail/session", response_model=Conversation)
async def mail_session(request: Request, body: MailSessionRequest) -> Conversation:
    """The chat beside a thread: the same one every time this thread is opened, its
    instructions refreshed with the thread as it is now."""
    try:
        return await core_of(request).maildesk.session(body.entry_id, body.account)
    except MailDeskError as exc:
        raise _desk_error(exc) from exc


# --- meeting auto-RSVP ------------------------------------------------------------------------


@router.get("/rsvp/state")
async def rsvp_state(request: Request) -> dict[str, Any]:
    core = core_of(request)
    state = await core.rsvp.state()
    return {
        "enabled": core.settings.rsvp.enabled,
        "state": state.model_dump(mode="json") if state else None,
        "recent": await core.rsvp.decisions(limit=50),
    }


@router.post("/rsvp/run")
async def rsvp_run(request: Request, dry_run: bool = False) -> dict[str, Any]:
    """Run one RSVP pass now. ``?dry_run=true`` reports what would be answered and how, sending
    nothing and recording nothing."""
    report = await core_of(request).rsvp.run_once(dry_run=dry_run)
    return {
        "dry_run": report.dry_run,
        "account": report.account,
        "pending": report.pending,
        "removed_canceled": report.removed_canceled,
        "errors": report.errors,
        "decisions": [d.model_dump(mode="json") for d in report.decisions],
    }


# --- shadow decisions (features/shadow.py) ----------------------------------------------------


@router.get("/shadow/stats")
async def shadow_stats(request: Request) -> dict[str, Any]:
    """Rows per decision point and source, Laya's answer and error counts, mean latencies."""
    core = core_of(request)
    return {"settings": core.settings.shadow.model_dump(mode="json"), **await core.shadow.stats()}


@router.get("/shadow/rows")
async def shadow_rows(request: Request, since_id: int = 0, limit: int = 1000) -> dict[str, Any]:
    """The recorded rows after ``since_id``, oldest first - page through with the last id."""
    rows = await core_of(request).shadow.rows(since_id=since_id, limit=max(1, min(limit, 5000)))
    return {"rows": rows, "last_id": rows[-1]["id"] if rows else since_id}
