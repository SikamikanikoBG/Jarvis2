"""Meeting capture with fake audio sources (no microphone, no Windows)."""

from __future__ import annotations

import audioop
import base64
import io
import math
import struct
import wave

import pytest

from jarvis_host.meetings import TARGET_RATE, AudioSource, MeetingCapture, MeetingError, describe_levels


def tone(seconds: float, rate: int, channels: int, freq: float = 220.0, amp: int = 8000) -> bytes:
    frames = int(seconds * rate)
    out = bytearray()
    for i in range(frames):
        value = int(amp * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("<" + "h" * channels, *([value] * channels))
    return bytes(out)


class FakeSource(AudioSource):
    """Hands out whatever was queued, like a device buffer between two pulls."""

    def __init__(self, rate: int, channels: int) -> None:
        self.rate = rate
        self.channels = channels
        self.peak = 0
        self.queued = bytearray()
        self.closed = False

    def push(self, seconds: float, amp: int = 8000) -> None:
        data = tone(seconds, self.rate, self.channels, amp=amp)
        self.queued += data
        self.peak = max(self.peak, audioop.rms(data, 2))

    def read(self) -> bytes:
        data = bytes(self.queued)
        self.queued.clear()
        return data

    def close(self) -> None:
        self.closed = True


def wav_frames(b64: str) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as wf:
        return wf.getnchannels(), wf.getframerate(), wf.getnframes()


@pytest.fixture
def capture() -> tuple[MeetingCapture, dict[str, FakeSource]]:
    made: dict[str, FakeSource] = {}

    def opener(wanted: tuple[str, ...]) -> tuple[dict[str, AudioSource], list[str]]:
        # The real devices on this laptop: 44.1 kHz stereo microphone, 48 kHz stereo loopback.
        rates = {"mic": 44_100, "system": 48_000}
        problems: list[str] = []
        opened: dict[str, AudioSource] = {}
        for name in wanted:
            if name in made:  # a second meeting on the same fake devices
                opened[name] = made[name]
                continue
            src = FakeSource(rates[name], 2)
            made[name] = src
            opened[name] = src
        return opened, problems

    return MeetingCapture(opener), made


def test_start_mixes_both_sources_into_16k_mono_chunks(capture):  # type: ignore[no-untyped-def]
    cap, sources = capture
    started = cap.start("mtg_1")
    assert started["recording"] is True and started["sources"] == ["mic", "system"] and started["rate"] == TARGET_RATE
    assert cap.active() == ["mtg_1"]

    # Nothing recorded yet: no chunk rather than an empty WAV.
    assert cap.pull("mtg_1")["chunks"] == []

    sources["mic"].push(2.0)
    sources["system"].push(2.0)
    res = cap.pull("mtg_1")
    chunk = res["chunks"][0]
    channels, rate, frames = wav_frames(chunk["wav_base64"])
    assert (channels, rate) == (1, TARGET_RATE)
    assert abs(frames - 2 * TARGET_RATE) < 200  # resampling rounds; 2 seconds ± a few ms
    assert chunk["seq"] == 1 and chunk["t0"] == 0.0 and 1.9 < chunk["t1"] < 2.1

    # The next chunk continues where the first stopped: the core stitches on t0.
    sources["mic"].push(1.5)
    sources["system"].push(1.5)
    second = cap.pull("mtg_1")["chunks"][0]
    assert second["seq"] == 2 and abs(second["t0"] - chunk["t1"]) < 0.01


def test_a_short_burst_waits_but_the_stop_drains_it(capture):  # type: ignore[no-untyped-def]
    cap, sources = capture
    cap.start("mtg_2")
    sources["mic"].push(0.3)  # under a second: not worth an STT round trip yet
    assert cap.pull("mtg_2")["chunks"] == []
    sources["mic"].push(0.3)
    stopped = cap.stop("mtg_2")
    # Both bursts came back in the final chunk; nothing recorded is lost at the stop.
    channels, rate, frames = wav_frames(stopped["chunks"][0]["wav_base64"])
    assert (channels, rate) == (1, TARGET_RATE) and abs(frames - int(0.6 * TARGET_RATE)) < 200
    assert stopped["recording"] is False and 0.5 < stopped["seconds"] < 0.7
    assert all(s.closed for s in sources.values()) and cap.active() == []


def test_a_device_that_delivers_digital_silence_is_named(capture):  # type: ignore[no-untyped-def]
    """On this laptop both devices delivered exact zeros to the host process (the endpoint peak
    meter confirmed nothing was rendered). An empty transcript was the only symptom."""
    cap, sources = capture
    started = cap.start("mtg_lvl")
    assert started["levels"] == {"mic": 0, "system": 0}
    assert "microphone delivered digital silence" in started["warning"]
    assert "system audio delivered digital silence" in started["warning"]

    sources["mic"].push(1.5)  # the microphone comes alive
    res = cap.pull("mtg_lvl")
    assert res["levels"]["mic"] > 0 and res["levels"]["system"] == 0
    assert "microphone" not in (res["warning"] or "") and "system audio" in res["warning"]

    sources["system"].push(1.5)
    stopped = cap.stop("mtg_lvl")
    assert stopped["warning"] is None and all(v > 0 for v in stopped["levels"].values())
    assert describe_levels({}) is None


def test_silence_is_dropped_but_the_clock_keeps_running(capture):  # type: ignore[no-untyped-def]
    """The first live meeting transcribed a quiet room as two segments of "Thank you."."""
    cap, sources = capture
    cap.start("mtg_s")
    sources["mic"].queued += tone(2.0, 44_100, 2, amp=20)  # room tone, no speech
    sources["system"].queued += tone(2.0, 48_000, 2, amp=20)
    res = cap.pull("mtg_s")
    assert res["chunks"] == [] and res["silent_seconds"] >= 1.9

    sources["mic"].push(2.0)  # someone speaks
    sources["system"].push(2.0)
    chunk = cap.pull("mtg_s")["chunks"][0]
    # Dropped silence still moved the clock, so this chunk is timed where it really happened.
    assert chunk["seq"] == 1 and 1.9 < chunk["t0"] < 2.1


def test_one_dead_source_still_records_the_other_and_says_so():
    def opener(wanted: tuple[str, ...]) -> tuple[dict[str, AudioSource], list[str]]:
        return {"mic": FakeSource(16_000, 1)}, ["system: OSError: no loopback device"]

    cap = MeetingCapture(opener)
    started = cap.start("mtg_3")
    assert started["sources"] == ["mic"] and started["unavailable"] == ["system: OSError: no loopback device"]


def test_no_source_at_all_is_an_error_not_a_silent_recording():
    def opener(wanted: tuple[str, ...]) -> tuple[dict[str, AudioSource], list[str]]:
        raise MeetingError("no audio source could be opened — mic: OSError: none")

    with pytest.raises(MeetingError, match="no audio source"):
        MeetingCapture(opener).start("mtg_4")


def test_unknown_ids_and_sources_are_refused_clearly(capture):  # type: ignore[no-untyped-def]
    cap, _ = capture
    with pytest.raises(MeetingError, match="unknown source"):
        cap.start("mtg_5", sources="mic,webcam")
    with pytest.raises(MeetingError, match="not being recorded"):
        cap.pull("nope")
    # Stopping something that is not running is not an error: the core retries a stop.
    assert cap.stop("nope")["already_stopped"] is True


def test_starting_the_same_meeting_twice_does_not_open_a_second_stream(capture):  # type: ignore[no-untyped-def]
    cap, sources = capture
    cap.start("mtg_6")
    again = cap.start("mtg_6")
    assert again["already"] is True
    sources["mic"].push(1.2)
    assert cap.pull("mtg_6")["chunks"][0]["seq"] == 1  # one recording, one sequence


def test_close_all_releases_every_device(capture):  # type: ignore[no-untyped-def]
    cap, sources = capture
    cap.start("mtg_7")
    cap.close_all()
    assert cap.active() == [] and all(s.closed for s in sources.values())
