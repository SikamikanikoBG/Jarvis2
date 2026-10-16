"""The headless caller's microphone: test/question.wav, spoken by OmniVoice ("borislav").

    9 s silence (the greeting) · a request for a long story · 18 s (Jarvis thinks, then tells it)
    · a cut-in while he speaks · 12 s · a puzzle that makes him think · 35 s

    uv run python test/make_question.py
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

TTS = os.environ.get("CALL_TTS_URL", "http://100.97.120.53:9120") + "/tts"
RATE = 48_000


def say(text: str) -> np.ndarray:
    res = httpx.post(TTS, json={"text": text, "language": "en", "voice": "borislav", "format": "wav"}, timeout=60)
    res.raise_for_status()
    pcm, rate = sf.read(io.BytesIO(res.content), dtype="float32")
    n = int(len(pcm) * RATE / rate)
    return np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)


def silence(secs: float) -> np.ndarray:
    return np.zeros(int(secs * RATE))


def main() -> None:
    track = np.concatenate([
        silence(9),
        say("Jarvis, tell me a long story about the history of Sofia, with lots of detail please."),
        silence(18),
        say("Stop, stop. Sorry to cut you off. What is two plus two?"),
        silence(12),
        say("A puzzle for you. A train leaves at three fifteen, drives two hundred and forty kilometres "
            "at eighty an hour, waits twenty minutes, then drives ninety kilometres at sixty. When does it arrive?"),
        silence(35),
    ])
    out = Path(__file__).parent / "question.wav"
    sf.write(out, (track / np.abs(track).max() * 0.8 * 32767).astype(np.int16), RATE, subtype="PCM_16")
    print(f"{out}: {len(track) / RATE:.1f} s")


if __name__ == "__main__":
    main()
