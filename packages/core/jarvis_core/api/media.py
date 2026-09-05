"""STT upload endpoint (meetings endpoints join it in features/meetings)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from jarvis_core.api.deps import core_of, require_token
from jarvis_core.features.stt import SttError

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
