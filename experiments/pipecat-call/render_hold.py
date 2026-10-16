"""Render Jarvis's hold music (web/src/voice/hold.ts) to a seamless WAV loop for the bot's mixer.

Same chords, tempo, envelopes and voices as the page's Web Audio version, rendered offline at
the call's output rate. The bot mixes it into the call's own audio track, so it reaches the phone
as the far end of the call and the browser's echo canceller has it as a reference.

    uv run python render_hold.py assets/hold.wav
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 24_000
BPM = 72
BEAT = 60 / BPM
BAR = 4 * BEAT
PROGRESSIONS = [
    [[53, 57, 60, 64], [52, 55, 59, 62], [50, 53, 57, 60], [48, 52, 55, 59]],
    [[57, 60, 64, 67], [50, 54, 57, 64], [55, 59, 62, 66], [48, 52, 55, 59]],
]
BARS = 16  # four bars per progression, each progression twice: one full cycle of hold.ts


def hz(midi: float) -> float:
    return 440 * 2 ** ((midi - 69) / 12)


def envelope(points: list[tuple[float, float]], n: int) -> np.ndarray:
    """Exponential ramps between (time, level) points, like AudioParam.exponentialRampToValueAtTime."""
    t = np.arange(n) / RATE
    out = np.zeros(n)
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        mask = (t >= t0) & (t < t1)
        x = (t[mask] - t0) / (t1 - t0)
        out[mask] = v0 * (v1 / v0) ** x
    return out


def triangle(phase: np.ndarray) -> np.ndarray:
    return 2 * np.abs(2 * (phase - np.floor(phase + 0.5))) - 1


def add(buf: np.ndarray, at: float, sig: np.ndarray) -> None:
    """Add a note starting at ``at`` seconds; what runs past the loop's end wraps to its start."""
    start = int(at * RATE)
    idx = (start + np.arange(len(sig))) % len(buf)
    np.add.at(buf, idx, sig)


def keys(buf: np.ndarray, freq: float, at: float, dur: float, peak: float) -> None:
    n = int((dur + 0.05) * RATE)
    t = np.arange(n) / RATE
    env = envelope([(0, 1e-4), (0.02, peak), (min(0.6, dur * 0.4), peak * 0.35), (dur, 1e-4), (dur + 0.05, 1e-4)], n)
    tone = np.sin(2 * np.pi * freq * t) + 0.18 * triangle(2 * freq * t)
    add(buf, at, tone * env)


def bass(buf: np.ndarray, freq: float, at: float, dur: float) -> None:
    n = int((dur + 0.05) * RATE)
    t = np.arange(n) / RATE
    env = envelope([(0, 1e-4), (0.03, 0.14), (dur, 1e-4), (dur + 0.05, 1e-4)], n)
    add(buf, at, np.sin(2 * np.pi * freq * t) * env)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """Second-order Butterworth low-pass (the page's BiquadFilter, Q≈0.707), run twice round
    the loop so its start carries the state of its end."""
    w0 = 2 * np.pi * cutoff / RATE
    alpha = np.sin(w0) / (2 * 0.7071)
    cos = np.cos(w0)
    b = np.array([(1 - cos) / 2, 1 - cos, (1 - cos) / 2]) / (1 + alpha)
    a = np.array([1, -2 * cos / (1 + alpha), (1 - alpha) / (1 + alpha)])
    y = np.zeros(2 * len(x))
    xx = np.concatenate([x, x])
    x1 = x2 = y1 = y2 = 0.0
    for i, v in enumerate(xx):
        out = b[0] * v + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        x2, x1, y2, y1 = x1, v, y1, out
        y[i] = out
    return y[len(x) :]


def render(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    buf = np.zeros(int(BARS * BAR * RATE))
    for bar in range(BARS):
        at = bar * BAR
        chord = PROGRESSIONS[(bar // 4) % len(PROGRESSIONS)][bar % 4]
        for note in chord:
            keys(buf, hz(note), at, BAR * 0.95, 0.09)
            keys(buf, hz(note), at + 2 * BEAT, BAR * 0.45, 0.045)
        root = chord[0]
        bass(buf, hz(root - 12), at, BEAT * 1.6)
        bass(buf, hz(root - 12), at + 2 * BEAT, BEAT * 1.4)
        for i in range(8):
            if i % 2 == 0 or rng.random() < 0.45:
                continue
            note = chord[int(rng.random() * len(chord))]
            keys(buf, hz(note + 12), at + i * BEAT / 2, BEAT * 1.2, 0.05)
    return lowpass(buf * 0.5, 2200)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/hold.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    audio = render()
    audio = audio / max(1e-9, np.abs(audio).max()) * 0.9
    sf.write(out, (audio * 32767).astype(np.int16), RATE, subtype="PCM_16")
    print(f"{out}: {len(audio) / RATE:.1f} s at {RATE} Hz")


if __name__ == "__main__":
    main()
