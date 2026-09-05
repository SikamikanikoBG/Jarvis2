"""Speech to text through the Whisper service. No fallback: a failing backend is an error and a
red badge, never a different model (V1 served a backup model for three days in silence)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_MODEL_MAP = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large": "Systran/faster-whisper-large-v3",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}


class SttError(RuntimeError):
    pass


@dataclass(slots=True)
class SttResult:
    text: str
    language: str | None
    backend: str
    duration_ms: int
    warning: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)


class Transcriber:
    def __init__(self, settings: Any) -> None:
        self._settings = settings  # Callable[[], Settings]
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=300.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _endpoint(self, url: str, kind: str) -> str:
        url = url.rstrip("/")
        if kind == "asr":
            return url if url.endswith("/asr") else url + "/asr"
        if url.endswith("/v1/audio/transcriptions"):
            return url
        return (url if url.endswith("/v1") else url + "/v1") + "/audio/transcriptions"

    async def transcribe(self, audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        s = self._settings()
        if not s.stt_url:
            raise SttError("no STT backend configured (settings.stt_url)")
        kind = s.stt_kind
        endpoint = self._endpoint(s.stt_url, kind)
        t0 = time.perf_counter()
        try:
            if kind == "asr":
                params: dict[str, str] = {"output": "json", "task": "transcribe"}
                if language:
                    params["language"] = language
                resp = await self._client.post(endpoint, params=params, files={"audio_file": (filename, audio, mime)})
            else:
                model = str(s.stt_model)
                data: dict[str, str] = {
                    "model": _MODEL_MAP.get(model, model),
                    "response_format": "verbose_json",
                }
                if language:
                    data["language"] = language
                resp = await self._client.post(endpoint, data=data, files={"file": (filename, audio, mime)})
        except httpx.HTTPError as exc:
            raise SttError(f"STT backend unreachable: {type(exc).__name__}: {exc}") from exc
        ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code >= 400:
            raise SttError(f"STT backend HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise SttError("STT backend returned non-JSON") from exc
        text = str(body.get("text") or "").strip()
        lang = body.get("language")
        segments = [
            {"t0": seg.get("start"), "t1": seg.get("end"), "text": seg.get("text", "")}
            for seg in body.get("segments", []) or []
        ]
        # The nginx proxy in front of the Whisper backends reports who actually answered.
        chain = [p.strip() for p in (resp.headers.get("X-Whisper-Upstream") or "").split(",") if p.strip()]
        warning = None
        if len(chain) > 1:
            warning = f"served by fallback upstream {chain[-1]} after {', '.join(chain[:-1])} failed"
            log.warning("stt: %s", warning)
        backend = f"{kind}:{chain[-1] if chain else s.stt_url}"
        return SttResult(text=text, language=lang, backend=backend, duration_ms=ms, warning=warning, segments=segments)
