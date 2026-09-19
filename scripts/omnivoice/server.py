"""A voice of Jarvis's own, on ardi's 3090 (docs/stories/10_voice.md).

OmniVoice (k2-fsa, Apache-2.0) speaks 600+ languages, Bulgarian among them, from a few
seconds of any voice. This is the thinnest server around it: the model resident in fp16,
one voice-clone prompt per reference clip in ``voices/`` (``name.wav`` + ``name.txt``, the
words spoken in the clip), one sentence in, one MP3 out. Jarvis's core calls it when
``Settings.voice.engine`` is "omnivoice"; nothing else knows it exists.

    GET  /health            -> {"ok": true, "voices": [...]}
    GET  /voices            -> the reference voices found (rescanned each call)
    POST /tts               -> audio/mpeg (or audio/wav with "format": "wav")
         {"text": "...", "language": "bg", "voice": "borislav", "steps": 16, "speed": 1.0}

A voice named ``design:<items>`` is designed instead of cloned, from OmniVoice's own vocabulary
("male, middle-aged, low pitch"); see its README for the list.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger("omnivoice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

VOICES = Path(os.environ.get("VOICES_DIR", "/voices"))
MODEL_ID = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
SAMPLE_RATE = 24_000
MAX_CHARS = 600

app = FastAPI(title="omnivoice")
_state: dict[str, Any] = {"model": None}
_prompts: dict[str, tuple[float, Any]] = {}  # name -> (mtime of the clip, prompt)
_lock = asyncio.Lock()


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CHARS)
    language: str | None = None
    voice: str = "borislav"
    steps: int = Field(default=16, ge=4, le=64)
    speed: float | None = Field(default=None, gt=0.5, lt=2.0)
    format: str = "mp3"


def _scan() -> list[str]:
    """Reference voices on disk; a new or changed clip gets a fresh prompt."""
    found: list[str] = []
    for wav in sorted(VOICES.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists():
            continue
        found.append(wav.stem)
        mtime = wav.stat().st_mtime
        if wav.stem in _prompts and _prompts[wav.stem][0] == mtime:
            continue
        t0 = time.perf_counter()
        prompt = _state["model"].create_voice_clone_prompt(ref_audio=str(wav), ref_text=txt.read_text(encoding="utf-8").strip())
        _prompts[wav.stem] = (mtime, prompt)
        log.info("voice %s ready in %.1fs", wav.stem, time.perf_counter() - t0)
    for gone in set(_prompts) - set(found):
        del _prompts[gone]
    return found


@app.on_event("startup")
def _load() -> None:
    from omnivoice import OmniVoice

    t0 = time.perf_counter()
    _state["model"] = OmniVoice.from_pretrained(MODEL_ID, device_map="cuda:0", dtype=torch.float16)
    log.info("model loaded in %.1fs, %d MB", time.perf_counter() - t0, torch.cuda.memory_allocated() // 2**20)
    voices = _scan()
    # The first generation pays for kernels and caches; pay it now, not on the first sentence.
    if voices:
        _state["model"].generate(text="Проба.", language="bg", voice_clone_prompt=_prompts[voices[0]][1])
    log.info("warm; voices: %s", ", ".join(voices) or "none")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": _state["model"] is not None, "voices": list(_prompts), "vram_mb": torch.cuda.memory_allocated() // 2**20}


@app.get("/voices")
def voices() -> dict[str, Any]:
    return {"voices": _scan()}


def _generate(req: TtsRequest) -> np.ndarray:
    from omnivoice.models.omnivoice import OmniVoiceGenerationConfig

    kw: dict[str, Any] = {"generation_config": OmniVoiceGenerationConfig(num_step=req.steps)}
    if req.voice.startswith("design:"):
        kw["instruct"] = req.voice[len("design:") :].strip()
    else:
        _scan()
        if req.voice not in _prompts:
            raise HTTPException(404, f"no voice {req.voice!r}; have {sorted(_prompts)}")
        kw["voice_clone_prompt"] = _prompts[req.voice][1]
    if req.speed:
        kw["speed"] = req.speed
    lang = (req.language or "").split("-")[0].lower() or None
    out = _state["model"].generate(text=" ".join(req.text.split()), language=lang, **kw)
    return np.asarray(out[0], dtype=np.float32).squeeze()


def _encode(wav: np.ndarray, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    sf.write(buf, wav, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    if fmt == "wav":
        return buf.getvalue(), "audio/wav"
    mp3 = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        input=buf.getvalue(), capture_output=True, check=True,
    ).stdout
    return mp3, "audio/mpeg"


@app.post("/tts")
async def tts(req: TtsRequest) -> Response:
    if _state["model"] is None:
        raise HTTPException(503, "model not loaded yet")
    t0 = time.perf_counter()
    async with _lock:  # one sentence at a time on the card; the core already limits itself to two
        wav = await asyncio.get_running_loop().run_in_executor(None, _generate, req)
    data, media = await asyncio.get_running_loop().run_in_executor(None, _encode, wav, req.format)
    log.info("%s %s %d chars -> %.1fs audio in %.2fs", req.voice, req.language, len(req.text), len(wav) / SAMPLE_RATE, time.perf_counter() - t0)
    return Response(content=data, media_type=media)
