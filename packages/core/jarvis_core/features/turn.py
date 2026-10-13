"""Has he finished? End-of-turn detection for a call (docs/stories/12_assistant.md).

A call used to decide that Arsen had finished by the clock alone: half a second of silence
closed the utterance, and another 0.6 s waited in case he went on. That is right for "and then
I..." and pure waiting for "thanks, that's all". Smart Turn v3 (pipecat-ai, BSD-2-Clause) scores
the last 8 seconds of audio - the waveform, so the intonation, not the words - for whether the
speaker has finished: 8 MB of ONNX on the core's CPU, about a tenth of a second. The call sends at
once when it is sure; when it is not, the clock decides as before.

Measured on ardi 2026-10-13 with Bulgarian speech: complete sentences 0.87-0.97, sentences cut
off mid-way 0.06-0.17, and the odd miss both ways (a clipped "не съм, не съм" scored 0.01). Hence
a high threshold, and a miss costs nothing that the steer does not already absorb.

The model is fetched once, pinned to a revision and checked against its hash, into
``<home>/models``. No numpy/onnxruntime, no model, or any failure: ``score`` returns None and the
call behaves as it always did.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import logging
import wave
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
MODEL_URL = (
    "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/"
    "f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-cpu.onnx"
)
MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"
RATE = 16_000
WINDOW_S = 8


class TurnDetector:
    def __init__(self, home: Path, *, fetch: bool = False) -> None:
        self._path = home / "models" / MODEL_FILE
        self._fetch = fetch  # may download the model when it is not on the disk yet
        self._session: Any = None
        self._failed = False
        self._loading: asyncio.Task[None] | None = None
        # One inference at a time: it is a tenth of a second of one core, and two calls at once
        # would only queue inside onnxruntime instead of here.
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._session is not None

    def warm(self) -> None:
        """Fetch and load the model in the background (at startup), never on a call's path."""
        if not self._fetch and not self._path.exists():
            return
        if self._session is None and not self._failed and self._loading is None:
            self._loading = asyncio.create_task(self._load(), name="turn-model")

    async def _load(self) -> None:
        try:
            if not self._path.exists():
                await self._download()
            self._session = await asyncio.to_thread(_open_session, self._path)
            log.info("turn detector ready (%s)", self._path.name)
        except Exception as exc:  # optional: the call works without it
            self._failed = True
            log.warning("turn detector unavailable: %s", exc)

    async def _download(self) -> None:
        import httpx

        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
            res = await client.get(MODEL_URL)
            res.raise_for_status()
        data = res.content
        digest = hashlib.sha256(data).hexdigest()
        if digest != MODEL_SHA256:
            raise RuntimeError(f"{MODEL_FILE}: hash {digest[:12]}… is not the pinned one")
        tmp = self._path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(self._path)

    async def score(self, wav: bytes) -> float | None:
        """How sure the model is that the speech in ``wav`` (16 kHz mono PCM WAV) is a finished
        turn, 0..1; None when it cannot say."""
        if self._session is None:
            self.warm()
            return None
        try:
            samples = _pcm16(wav)
        except (wave.Error, EOFError, ValueError):
            return None
        if samples is None:
            return None
        async with self._lock:
            try:
                return await asyncio.to_thread(_predict, self._session, samples)
            except Exception as exc:
                log.warning("turn detector failed: %s", exc)
                return None


def wav_from(wav: bytes, offset_ms: int) -> bytes:
    """The same WAV from ``offset_ms`` on; anything that is not a WAV comes back as it was."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as r:
            params = r.getparams()
            skip = min(r.getnframes(), int(params.framerate * offset_ms / 1000))
            r.setpos(skip)
            frames = r.readframes(r.getnframes() - skip)
    except (wave.Error, EOFError):
        return wav
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
    return out.getvalue()


def _open_session(path: Path) -> Any:
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 2
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


def _pcm16(wav: bytes) -> Any:
    """16 kHz mono PCM16 WAV → float32 samples, or None for anything else (the call's own
    listener writes exactly that; other formats are not worth a decoder here)."""
    import numpy as np

    with wave.open(io.BytesIO(wav), "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            return None
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def _predict(session: Any, samples: Any) -> float:
    import numpy as np

    n = RATE * WINDOW_S
    # The last 8 s, zero-padded at the FRONT: the model was trained with the end of the turn at
    # the end of the window. Padding at the back puts seconds of silence after every sentence and
    # everything scores "finished" (found the hard way while measuring, 2026-10-13).
    x = samples[-n:]
    if x.size < n:
        x = np.pad(x, (n - x.size, 0))
    features = _log_mel(x)[None, :, :]
    out = session.run(None, {"input_features": features})
    return float(np.asarray(out[0]).reshape(-1)[0])


# --- Whisper log-mel features, numpy only ---------------------------------------------------
# Vendored from pipecat (src/pipecat/audio/turn/smart_turn/_whisper_features.py, BSD-2-Clause,
# Copyright (c) 2024-2026 Daily), itself derived from Hugging Face Transformers'
# WhisperFeatureExtractor (Apache-2.0). It replicates WhisperFeatureExtractor(chunk_length=8)
# with do_normalize=True, so the model sees exactly the features it was trained on.

_N_FFT = 400
_HOP = 160
_N_MELS = 80
_MEL_FLOOR = 1e-10
_EPS = 1e-7


def _hz_to_mel(freq: Any) -> Any:
    import numpy as np

    freq = np.atleast_1d(np.asarray(freq, dtype=np.float64))
    mels = 3.0 * freq / 200.0
    log_region = freq >= 1000.0
    mels[log_region] = 15.0 + np.log(freq[log_region] / 1000.0) * (27.0 / np.log(6.4))
    return mels


def _mel_to_hz(mels: Any) -> Any:
    import numpy as np

    mels = np.atleast_1d(np.asarray(mels, dtype=np.float64))
    freq = 200.0 * mels / 3.0
    log_region = mels >= 15.0
    freq[log_region] = 1000.0 * np.exp((np.log(6.4) / 27.0) * (mels[log_region] - 15.0))
    return freq


@functools.cache
def _constants() -> tuple[Any, Any]:
    """The mel filterbank and the Hann window, built once."""
    import numpy as np

    bins = _N_FFT // 2 + 1
    mel_freqs = np.linspace(float(_hz_to_mel(0.0)[0]), float(_hz_to_mel(RATE / 2.0)[0]), _N_MELS + 2)
    filter_freqs = _mel_to_hz(mel_freqs)
    fft_freqs = np.linspace(0, RATE // 2, bins)
    diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    filters = np.maximum(np.zeros(1), np.minimum(down, up))
    filters *= np.expand_dims(2.0 / (filter_freqs[2 : _N_MELS + 2] - filter_freqs[:_N_MELS]), 0)
    return filters, np.hanning(_N_FFT + 1)[:-1]


def _log_mel(audio: Any) -> Any:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    filters, window = _constants()
    x = np.asarray(audio, dtype=np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + _EPS)
    pad = _N_FFT // 2
    padded = np.pad(x.astype(np.float64), (pad, pad), mode="reflect")
    frames = sliding_window_view(padded, _N_FFT)[::_HOP]
    power = (np.abs(np.fft.rfft(frames * window, axis=-1)) ** 2).T
    mel = np.maximum(_MEL_FLOOR, filters.T @ power)
    log_spec = np.log10(mel)[:, :-1]
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)
