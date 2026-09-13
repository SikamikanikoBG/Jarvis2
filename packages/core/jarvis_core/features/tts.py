"""Text to speech for a call (docs/stories/10_voice.md): one sentence in, one MP3 out.

The device's own voices are what the phone and the laptop happen to ship - a 2012 Bulgarian on
Android, a robot on Windows. The core synthesises with Microsoft's neural voices instead
(``edge-tts``, the same engine the Edge browser reads aloud with), a sentence at a time, and the
browser plays the bytes itself. That last part matters beyond the sound: audio the page plays
is audio the browser's echo canceller can subtract from the microphone, and on Android it stays
on the call's route instead of hopping between the TTS engine's speaker and the earpiece.

Sentences are cached on disk by (voice, rate, text): the second "Разбрах." is free.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis_proto import Settings

log = logging.getLogger(__name__)

MAX_CHARS = 600  # a sentence, with room for a long one; the client splits, this is the ceiling
TIMEOUT_S = 12.0
CACHE_FILES = 2_000  # oldest evicted past this; a sentence is ~30 kB


class TtsError(RuntimeError):
    pass


class Synthesizer:
    def __init__(self, settings: Callable[[], Settings], home: Path) -> None:
        self._settings = settings
        self._dir = home / "tts"
        # Two at once: one sentence being fetched while the previous one plays is the whole
        # point; more than that and a long answer floods the endpoint for audio nobody hears yet.
        self._sem = asyncio.Semaphore(2)

    async def synthesize(self, text: str, lang: str) -> tuple[bytes, str]:
        text = " ".join(text.split())
        if not text:
            raise TtsError("nothing to say")
        if len(text) > MAX_CHARS:
            raise TtsError(f"a sentence of {len(text)} characters; the ceiling is {MAX_CHARS}")
        s = self._settings().voice
        voice = s.voice_for(lang)
        if not voice:
            raise TtsError(f"no voice configured for {lang!r}")
        key = hashlib.sha256(f"{voice}|{s.rate}|{text}".encode()).hexdigest()
        path = self._dir / f"{key}.mp3"
        if path.exists():
            return path.read_bytes(), "audio/mpeg"
        async with self._sem:
            if path.exists():  # a twin request got there first
                return path.read_bytes(), "audio/mpeg"
            audio = await asyncio.wait_for(_edge(text, voice, s.rate), TIMEOUT_S)
        if not audio:
            raise TtsError("the voice service returned no audio")
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(path)
        self._evict()
        return audio, "audio/mpeg"

    def _evict(self) -> None:
        try:
            files = sorted(self._dir.glob("*.mp3"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return
        for old in files[: max(0, len(files) - CACHE_FILES)]:
            with contextlib.suppress(OSError):
                old.unlink()


async def _edge(text: str, voice: str, rate: str) -> bytes:
    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise TtsError("edge-tts is not installed") from exc
    try:
        communicate: Any = edge_tts.Communicate(text, voice, rate=rate)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        return b"".join(chunks)
    except TtsError:
        raise
    except Exception as exc:
        raise TtsError(f"voice service failed: {type(exc).__name__}: {str(exc)[:160]}") from exc
