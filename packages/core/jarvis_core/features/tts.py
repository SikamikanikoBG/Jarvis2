"""Text to speech for a call (docs/stories/10_voice.md): one sentence in, one MP3 out.

The device's own voices are what the phone and the laptop happen to ship - a 2012 Bulgarian on
Android, a robot on Windows. The core synthesises with Microsoft's neural voices instead
(``edge-tts``, the same engine the Edge browser reads aloud with), a sentence at a time, and the
browser plays the bytes itself. Since 2026-09-19 there is a second engine, ``omnivoice``: Jarvis's
own voice on ardi's 3090 (scripts/omnivoice), a clone of any reference clip, nothing leaving the
house; a sentence it cannot get (server down, voice missing) is said by edge instead. That last part matters beyond the sound: audio the page plays
is audio the browser's echo canceller can subtract from the microphone, and on Android it stays
on the call's route instead of hopping between the TTS engine's speaker and the earpiece.

Sentences are cached on disk by (engine, voice, rate, text): the second "Разбрах." is free.
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

    async def synthesize(self, text: str, lang: str, *, cache: bool = True) -> tuple[bytes, str]:
        """``cache=False`` is an incognito call: a sentence already in the cache is still served
        from it (nothing new is learned by reading), but nothing said in that call is written."""
        text = " ".join(text.split())
        if not text:
            raise TtsError("nothing to say")
        if len(text) > MAX_CHARS:
            raise TtsError(f"a sentence of {len(text)} characters; the ceiling is {MAX_CHARS}")
        s = self._settings().voice
        local = s.engine == "omnivoice" and s.omnivoice_voice_for(lang)
        voice = local or s.voice_for(lang)
        if not voice:
            raise TtsError(f"no voice configured for {lang!r}")
        ident = f"omnivoice|{voice}|{s.omnivoice_steps}" if local else voice
        key = hashlib.sha256(f"{ident}|{s.rate}|{text}".encode()).hexdigest()
        path = self._dir / f"{key}.mp3"
        if path.exists():
            return path.read_bytes(), "audio/mpeg"
        async with self._sem:
            if path.exists():  # a twin request got there first
                return path.read_bytes(), "audio/mpeg"
            if local:
                try:
                    audio = await _omnivoice(s.omnivoice_url, text, lang, voice, s.omnivoice_steps)
                except TtsError as exc:
                    # The house voice is down: say it with the cloud one rather than fall silent.
                    log.warning("omnivoice failed, falling back to edge: %s", exc)
                    fallback = s.voice_for(lang)
                    if not fallback:
                        raise
                    audio = await asyncio.wait_for(_edge(text, fallback, s.rate), TIMEOUT_S)
                    cache = False  # not the voice the key names
            else:
                audio = await asyncio.wait_for(_edge(text, voice, s.rate), TIMEOUT_S)
        if not audio:
            raise TtsError("the voice service returned no audio")
        if not cache:
            return audio, "audio/mpeg"
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


async def _omnivoice(url: str, text: str, lang: str, voice: str, steps: int) -> bytes:
    """One sentence from the local server (scripts/omnivoice/server.py): MP3 bytes."""
    import httpx

    body = {"text": text, "language": (lang or "").split("-")[0].lower() or None, "voice": voice, "steps": steps}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            res = await client.post(url.rstrip("/") + "/tts", json=body)
    except httpx.HTTPError as exc:
        raise TtsError(f"omnivoice unreachable: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if res.status_code != 200:
        raise TtsError(f"omnivoice HTTP {res.status_code}: {res.text[:160]}")
    return res.content


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
