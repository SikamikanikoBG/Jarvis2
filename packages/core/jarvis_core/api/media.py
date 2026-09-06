"""STT upload, meetings, triage endpoints (docs/API.md)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jarvis_core.api.deps import core_of, require_token
from jarvis_core.features.stt import SttError
from jarvis_proto import Meeting, MeetingDetail, TriageState

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
    }


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
