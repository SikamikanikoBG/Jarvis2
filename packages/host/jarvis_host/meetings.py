"""Meeting capture: microphone + system audio (WASAPI loopback), mixed to 16 kHz mono WAV.

The core owns the meeting; the host only captures. It pulls with ``meeting_pull`` every few
seconds, so this module hands out whatever has accumulated since the last pull as one WAV chunk
with its offset from the start of the recording, and the core transcribes it.

Facts that shaped it:

* system audio needs its OWN WASAPI loopback stream (pyaudiowpatch); the microphone stream
  cannot see it. Each device runs at its own rate (48 kHz stereo loopback, 44.1 kHz microphone
  on this laptop), so both are resampled to 16 kHz mono here rather than by ffmpeg - the
  resampler state is carried between chunks, otherwise every chunk boundary clicks.
* one source failing is not the meeting failing: a laptop with no working loopback still records
  the microphone, and the reply says which sources are live.
* audio arrives on PyAudio's own thread; the buffers are drained under a lock.
"""

from __future__ import annotations

import audioop  # deprecated in 3.12, removed in 3.13 — the host pins >=3.12,<3.13 (pyproject)
import base64
import io
import logging
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

TARGET_RATE = 16_000
SAMPLE_WIDTH = 2  # int16
MIN_CHUNK_S = 1.0  # below this a chunk is not worth an STT round trip
MAX_BUFFER_S = 600  # if nobody pulls for ten minutes, drop the oldest audio rather than the host
# Whisper invents words for silence: the first live meeting produced two segments of "Thank you."
# from a quiet room. A chunk this quiet holds no speech, so it is dropped before it can be
# transcribed - the timeline still advances, so later chunks keep their real offsets.
# ~ -48 dBFS on the int16 scale; normal speech is 20-100x louder.
SILENCE_RMS = 120
START_LISTEN_S = 0.5  # sample the devices before answering meeting_start, to report their levels
# Silence is judged on the LOUDEST short window, never on the whole chunk: a 10-second pull that
# holds two seconds of a child speaking quietly averages out below any useful threshold. The
# first family recording lost 5 of its 7 chunks that way - 26 seconds of speech, gone.
WINDOW_S = 0.2


class MeetingError(RuntimeError):
    """Anything that stops a capture, phrased for the model."""


class AudioSource(Protocol):
    """A live PCM source. ``read`` drains what has arrived since the last call."""

    rate: int
    channels: int
    peak: int  # loudest RMS seen so far; 0 means the device delivered digital silence

    def read(self) -> bytes: ...
    def close(self) -> None: ...


class _Buffered:
    """Collects int16 frames from a callback thread; ``read`` drains them."""

    def __init__(self, rate: int, channels: int) -> None:
        self.rate = rate
        self.channels = channels
        # Loudest thing this source has delivered. A device that is muted, blocked by Windows
        # privacy settings or rendering to another endpoint delivers digital zeros, and without
        # this the only symptom is an empty transcript.
        self.peak = 0
        self._buf = bytearray()
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        limit = self.rate * self.channels * SAMPLE_WIDTH * MAX_BUFFER_S
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > limit:
                del self._buf[: len(self._buf) - limit]
        if data:
            self.peak = max(self.peak, audioop.rms(data, SAMPLE_WIDTH))

    def read(self) -> bytes:
        with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
        return data

    def close(self) -> None:  # pragma: no cover - overridden by the real source
        return None


class _PyAudioSource(_Buffered):
    """One WASAPI stream (microphone or default-speaker loopback) in callback mode."""

    def __init__(self, pa: Any, device: dict[str, Any]) -> None:
        import pyaudiowpatch as pyaudio  # pyright: ignore[reportMissingImports]

        rate = int(device["defaultSampleRate"])
        channels = max(1, min(2, int(device["maxInputChannels"])))
        super().__init__(rate, channels)
        self._pa = pa

        def callback(in_data: bytes, frame_count: int, time_info: Any, status: int) -> tuple[None, int]:
            self.feed(in_data)
            return (None, pyaudio.paContinue)

        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=2048,
            stream_callback=callback,
        )

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception as exc:  # pragma: no cover - a closing stream that argues
            log.warning("meeting: closing an audio stream failed: %s", exc)


def open_sources(sources: tuple[str, ...] = ("mic", "system")) -> tuple[dict[str, AudioSource], list[str]]:
    """Open the requested live sources. Returns what opened and why the rest did not."""
    import pyaudiowpatch as pyaudio  # pyright: ignore[reportMissingImports]

    pa = pyaudio.PyAudio()
    out: dict[str, AudioSource] = {}
    problems: list[str] = []
    for name in sources:
        try:
            device = pa.get_default_input_device_info() if name == "mic" else pa.get_default_wasapi_loopback()
            out[name] = _PyAudioSource(pa, device)
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    if not out:
        pa.terminate()
        raise MeetingError("no audio source could be opened — " + "; ".join(problems))
    return out, problems


@dataclass
class _Resampler:
    """Per-source conversion to 16 kHz mono, with the state that makes chunks seamless."""

    rate: int
    channels: int
    state: Any = None
    remainder: bytes = b""

    def convert(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        frame = SAMPLE_WIDTH * self.channels
        pcm = self.remainder + pcm
        usable = len(pcm) - (len(pcm) % frame)  # a half-frame tail would shift every later sample
        self.remainder, pcm = pcm[usable:], pcm[:usable]
        if not pcm:
            return b""
        mono = audioop.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5) if self.channels == 2 else pcm
        if self.rate == TARGET_RATE:
            return mono
        converted, self.state = audioop.ratecv(mono, SAMPLE_WIDTH, 1, self.rate, TARGET_RATE, self.state)
        return converted


@dataclass
class Recording:
    """One meeting being captured on this machine."""

    meeting_id: str
    sources: dict[str, AudioSource]
    problems: list[str] = field(default_factory=list)
    seq: int = 0
    emitted_frames: int = 0  # 16 kHz frames already handed to the core = the next chunk's t0
    silent_frames: int = 0  # dropped as silence; reported so a dead microphone is visible
    stopped: bool = False
    _resamplers: dict[str, _Resampler] = field(default_factory=dict)
    # Mixed audio that was too short to be worth a chunk. Draining the sources and then throwing
    # the result away would lose it: a 0.3 s burst before a pull must still reach the transcript.
    _pending: bytes = b""

    def _mix(self) -> bytes:
        tracks: list[bytes] = []
        for name, source in self.sources.items():
            resampler = self._resamplers.setdefault(name, _Resampler(source.rate, source.channels))
            track = resampler.convert(source.read())
            if track:
                tracks.append(track)
        live = [t for t in tracks if audioop.max(t, SAMPLE_WIDTH) > 0]
        if not live:
            return tracks[0] if tracks else b""
        if len(live) == 1:
            # Only one source is really producing sound (the other is muted, or nothing is
            # playing). Attenuating then would just make quiet speech quieter for no benefit.
            return live[0]
        longest = max(len(t) for t in live)
        mixed = live[0].ljust(longest, b"\x00")
        for track in live[1:]:
            # Halve first: two full-scale sources added together clip, and a clipped meeting
            # transcribes worse than a quiet one.
            mixed = audioop.add(
                audioop.mul(mixed, SAMPLE_WIDTH, 0.7),
                audioop.mul(track.ljust(longest, b"\x00"), SAMPLE_WIDTH, 0.7),
                SAMPLE_WIDTH,
            )
        return mixed

    def take_chunk(self, *, final: bool = False) -> dict[str, Any] | None:
        """The audio since the last call as a WAV chunk, or None when there is too little - or
        when it is silence, which Whisper would turn into invented words."""
        self._pending += self._mix()
        frames = len(self._pending) // SAMPLE_WIDTH
        if not frames or (not final and frames < MIN_CHUNK_S * TARGET_RATE):
            return None
        pcm, self._pending = self._pending, b""
        if loudest_window(pcm) < SILENCE_RMS:
            self.emitted_frames += frames  # keep the clock honest for the chunks that follow
            self.silent_frames += frames
            return None
        t0 = self.emitted_frames / TARGET_RATE
        self.emitted_frames += frames
        self.seq += 1
        return {
            "seq": self.seq,
            "t0": round(t0, 3),
            "t1": round(self.emitted_frames / TARGET_RATE, 3),
            "wav_base64": base64.b64encode(to_wav(pcm)).decode("ascii"),
        }

    def levels(self) -> dict[str, int]:
        """Peak RMS per source. All zeros = nothing reached this process; see `describe_levels`."""
        return {name: int(getattr(src, "peak", 0)) for name, src in self.sources.items()}

    def close(self) -> None:
        self.stopped = True
        for source in self.sources.values():
            source.close()


def describe_levels(levels: dict[str, int]) -> str | None:
    """A sentence about dead sources, or None when everything is delivering audio."""
    dead = sorted(name for name, peak in levels.items() if peak < SILENCE_RMS)
    if not dead or not levels:
        return None
    what = {
        "mic": "the microphone delivered digital silence (muted, or Windows microphone access is off for this app)",
        "system": "system audio delivered digital silence (nothing was playing through the "
        "speakers this machine captures, or the sound is going to another device)",
    }
    return "; ".join(what.get(name, f"{name} delivered digital silence") for name in dead)


def loudest_window(pcm: bytes, window_s: float = WINDOW_S) -> int:
    """RMS of the loudest ``window_s`` slice. Speech anywhere in a chunk keeps the whole chunk."""
    if not pcm:
        return 0
    step = max(SAMPLE_WIDTH, int(TARGET_RATE * window_s) * SAMPLE_WIDTH)
    if len(pcm) <= step:
        return audioop.rms(pcm, SAMPLE_WIDTH)
    return max(audioop.rms(pcm[i : i + step], SAMPLE_WIDTH) for i in range(0, len(pcm) - step + 1, step))


def to_wav(pcm: bytes, rate: int = TARGET_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class MeetingCapture:
    """The host side of a meeting: start, hand out chunks, stop. One recording per meeting id."""

    def __init__(
        self, opener: Callable[[tuple[str, ...]], tuple[dict[str, AudioSource], list[str]]] = open_sources
    ) -> None:
        self._opener = opener
        self._recordings: dict[str, Recording] = {}
        self._lock = threading.Lock()

    def start(self, meeting_id: str, sources: str = "mic,system") -> dict[str, Any]:
        wanted = tuple(s.strip().lower() for s in sources.split(",") if s.strip()) or ("mic", "system")
        unknown = [s for s in wanted if s not in ("mic", "system")]
        if unknown:
            raise MeetingError(f"unknown source(s) {', '.join(unknown)}; use 'mic', 'system' or both")
        with self._lock:
            if meeting_id in self._recordings:
                rec = self._recordings[meeting_id]
                return {"meeting_id": meeting_id, "recording": True, "sources": sorted(rec.sources), "already": True}
            opened, problems = self._opener(wanted)
            rec = Recording(meeting_id, opened, problems)
            self._recordings[meeting_id] = rec
        # Listen briefly before answering: "recording" while every device delivers zeros is the
        # one failure the UI cannot see, and it is worth half a second to say so up front.
        time.sleep(START_LISTEN_S)
        levels = rec.levels()
        return {
            "meeting_id": meeting_id,
            "recording": True,
            "sources": sorted(opened),
            "unavailable": problems,
            "rate": TARGET_RATE,
            "levels": levels,
            "warning": describe_levels(levels),
        }

    def pull(self, meeting_id: str, after_seq: int = 0) -> dict[str, Any]:
        rec = self._recordings.get(meeting_id)
        if rec is None:
            raise MeetingError(f"meeting {meeting_id} is not being recorded on this machine")
        chunk = rec.take_chunk()
        chunks = [chunk] if chunk and chunk["seq"] > after_seq else []
        return {
            "meeting_id": meeting_id,
            "recording": not rec.stopped,
            "chunks": chunks,
            "silent_seconds": round(rec.silent_frames / TARGET_RATE, 1),
            "levels": rec.levels(),
            "warning": describe_levels(rec.levels()),
        }

    def stop(self, meeting_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._recordings.pop(meeting_id, None)
        if rec is None:
            return {"meeting_id": meeting_id, "recording": False, "chunks": [], "already_stopped": True}
        chunk = rec.take_chunk(final=True)  # drain before the streams close
        rec.close()
        return {
            "meeting_id": meeting_id,
            "recording": False,
            "chunks": [chunk] if chunk else [],
            "seconds": round(rec.emitted_frames / TARGET_RATE, 1),
            "silent_seconds": round(rec.silent_frames / TARGET_RATE, 1),
            "levels": rec.levels(),
            "warning": describe_levels(rec.levels()),
        }

    def active(self) -> list[str]:
        return sorted(self._recordings)

    def close_all(self) -> None:
        with self._lock:
            recordings = list(self._recordings.values())
            self._recordings.clear()
        for rec in recordings:
            rec.close()
