"""The far end of a call (docs/stories/10_voice.md, "the line"): Jarvis's voice and the hold music
as the remote audio track of a WebRTC call.

Why a line at all. Until 2026-10-16 the page played the voice and the music itself, through Web
Audio. To Android that is media, not the far end of a call: the platform's echo canceller has
no reference for it, so on the speaker the microphone heard Jarvis and the music and Whisper
turned them into words ("Абонирайте се на нашия канал!"); the output's route and volume stream
depended on the order things were opened. Audio that arrives as a call's remote track is what
the phone's call stack is built for: it is the echo canceller's reference, it plays on the
call's route and the volume keys are the call's.

This server is only the loudspeaker at the far end. Nothing here decides what is said: the page
still runs the call (web voice/session.ts) - it fetches each sentence from the core's /api/tts
as before and hands the bytes over the call's data channel; the line plays them in order and
says when each one has been heard out, and mixes the hold music in while Jarvis is thinking.
The microphone stays in the page. No secrets: the core forwards the offer (POST
/api/voice/line/offer, behind its own token), the media goes straight to the phone.

    POST /offer   {"sdp", "type"} -> {"sdp", "type"}   (non-trickle: the page gathers first)
    GET  /health

Data channel "line", page -> line:
    {"t": "clip", "id": "...", "size": n}  then n bytes in binary messages: one sentence (MP3/WAV),
                                            played when the last byte is in, after those before it
    {"t": "stop"}                           cut: the sentence playing and those queued are dropped
    {"t": "pause"} / {"t": "resume"}        hold the voice mid-word / go on from that very spot
    {"t": "music", "on": bool, "volume": 0..1}
line -> page:
    {"t": "started", "id"} / {"t": "done", "id", "cut": bool} / {"t": "error", "id", "error"}
"""

from __future__ import annotations

import asyncio
import fractions
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field

import av
import numpy as np
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from hold import RATE as HOLD_RATE
from hold import render as render_hold

log = logging.getLogger("voiceline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("aioice").setLevel(logging.WARNING)  # every ICE candidate pair otherwise

RATE = 48_000
FRAME = RATE // 50  # 20 ms, Opus's own frame
FADE_IN_S = 1.6  # web voice/hold.ts: the music swells in, so a short wait barely hears it
FADE_OUT_S = 0.35
FLOOR = 1e-4  # where hold.ts's exponential ramps start and end
# Heard out: the last sample leaves here, then crosses the network and the phone's jitter buffer.
# "done" waits that long, so the page does not open the microphone on the tail of a sentence.
PLAYOUT_DELAY_S = 0.25


@dataclass
class Clip:
    id: str
    pcm: np.ndarray  # float32, -1..1, mono at RATE
    pos: int = 0
    started: bool = False


@dataclass
class Line:
    """One call's outgoing audio: the sentence queue and the music, mixed 20 ms at a time."""

    send: callable  # type: ignore[valid-type]  - JSON events to the page
    music: np.ndarray
    queue: list[Clip] = field(default_factory=list)
    music_on: bool = False
    music_volume: float = 0.4
    gain: float = FLOOR
    music_pos: int = 0
    paused: bool = False

    def next_frame(self) -> np.ndarray:
        out = np.zeros(FRAME, dtype=np.float32)
        filled = 0
        while filled < FRAME and self.queue and not self.paused:
            clip = self.queue[0]
            if not clip.started:
                clip.started = True
                self.send({"t": "started", "id": clip.id})
            n = min(FRAME - filled, len(clip.pcm) - clip.pos)
            out[filled : filled + n] = clip.pcm[clip.pos : clip.pos + n]
            clip.pos += n
            filled += n
            if clip.pos >= len(clip.pcm):
                self.queue.pop(0)
                self._done_later(clip.id, cut=False)
        self._mix_music(out)
        return out

    def _mix_music(self, out: np.ndarray) -> None:
        target = 1.0 if self.music_on else FLOOR
        if not self.music_on and self.gain <= FLOOR:
            return
        secs = FRAME / RATE
        step = (1 / FLOOR) ** (secs / (FADE_IN_S if self.music_on else FADE_OUT_S))
        end = min(target, self.gain * step) if target > self.gain else max(target, self.gain / step)
        ramp = np.geomspace(self.gain, end, FRAME, dtype=np.float32)
        self.gain = end
        idx = (self.music_pos + np.arange(FRAME)) % len(self.music)
        self.music_pos = (self.music_pos + FRAME) % len(self.music)
        # hold.ts: master gain 0.5 x the volume setting
        out += self.music[idx] * ramp * (0.5 * self.music_volume)

    def stop(self) -> None:
        self.paused = False
        for clip in self.queue:
            self.send({"t": "done", "id": clip.id, "cut": True})
        self.queue.clear()

    def _done_later(self, clip_id: str, *, cut: bool) -> None:
        asyncio.get_running_loop().call_later(PLAYOUT_DELAY_S, self.send, {"t": "done", "id": clip_id, "cut": cut})


class LineTrack(MediaStreamTrack):
    """The call's remote audio: real-time 20 ms frames, silence when there is nothing to say."""

    kind = "audio"

    def __init__(self, line: Line) -> None:
        super().__init__()
        self._line = line
        self._t0: float | None = None
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._t0 is None:
            self._t0 = time.monotonic()
        else:
            self._pts += FRAME
            wait = self._t0 + self._pts / RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        pcm = np.clip(self._line.next_frame(), -1.0, 1.0)
        frame = av.AudioFrame.from_ndarray((pcm * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


def decode(data: bytes) -> np.ndarray:
    """One sentence's bytes (MP3 from the core, or WAV) as float32 mono at the line's rate."""
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    chunks: list[np.ndarray] = []
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


MUSIC: np.ndarray | None = None
PCS: set[RTCPeerConnection] = set()


async def offer(request: web.Request) -> web.Response:
    body = await request.json()
    pc = RTCPeerConnection()
    PCS.add(pc)
    channel_box: dict[str, object] = {}

    def send(event: dict) -> None:
        ch = channel_box.get("ch")
        if ch is not None and ch.readyState == "open":  # type: ignore[attr-defined]
            ch.send(json.dumps(event))  # type: ignore[attr-defined]

    assert MUSIC is not None
    line = Line(send=send, music=MUSIC)
    track = LineTrack(line)
    call = f"{id(pc):x}"[-6:]

    @pc.on("datachannel")
    def on_datachannel(ch) -> None:  # noqa: ANN001
        channel_box["ch"] = ch
        pending: dict[str, object] = {}

        @ch.on("message")
        def on_message(message) -> None:  # noqa: ANN001
            if isinstance(message, bytes):
                if not pending:
                    return
                buf: bytearray = pending["buf"]  # type: ignore[assignment]
                buf.extend(message)
                if len(buf) >= pending["size"]:  # type: ignore[operator]
                    clip_id = str(pending["id"])
                    data = bytes(buf)
                    pending.clear()
                    asyncio.ensure_future(enqueue(clip_id, data))
                return
            try:
                msg = json.loads(message)
            except ValueError:
                return
            match msg.get("t"):
                case "clip":
                    pending.update(id=msg["id"], size=int(msg["size"]), buf=bytearray())
                case "stop":
                    pending.clear()
                    log.info("%s: cut (%d queued)", call, len(line.queue))
                    line.stop()
                case "diag":
                    # The page's own account (line.ts report): devices, where the sound goes.
                    log.info("%s: page %s", call, json.dumps({k: v for k, v in msg.items() if k != "t"}, ensure_ascii=False))
                case "pause" | "resume":
                    line.paused = msg["t"] == "pause"
                    log.info("%s: %s", call, msg["t"])
                case "music":
                    line.music_on = bool(msg.get("on"))
                    log.info("%s: music %s", call, "on" if line.music_on else "off")
                    if "volume" in msg:
                        line.music_volume = max(0.0, min(1.0, float(msg["volume"])))

    async def enqueue(clip_id: str, data: bytes) -> None:
        try:
            pcm = await asyncio.get_running_loop().run_in_executor(None, decode, data)
        except Exception as exc:  # a sentence that will not decode is skipped, and the page told
            log.warning("%s: clip %s did not decode: %s", call, clip_id, exc)
            send({"t": "error", "id": clip_id, "error": f"{type(exc).__name__}: {exc}"})
            send({"t": "done", "id": clip_id, "cut": False})
            return
        log.info("%s: clip %s, %.1f s", call, clip_id, len(pcm) / RATE)
        line.queue.append(Clip(clip_id, pcm))

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        log.info("%s: %s", call, pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            track.stop()
            await pc.close()
            PCS.discard(pc)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type=body["type"]))
    pc.addTrack(track)
    await pc.setLocalDescription(await pc.createAnswer())
    log.info("%s: call offered", call)
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": MUSIC is not None, "calls": len(PCS)})


async def on_startup(_: web.Application) -> None:
    global MUSIC
    t0 = time.perf_counter()
    assert HOLD_RATE == RATE
    MUSIC = await asyncio.get_running_loop().run_in_executor(None, render_hold)
    log.info("hold music rendered: %.1f s in %.1f s", len(MUSIC) / RATE, time.perf_counter() - t0)


async def on_shutdown(_: web.Application) -> None:
    await asyncio.gather(*(pc.close() for pc in list(PCS)))


def main() -> None:
    app = web.Application()
    app.router.add_post("/offer", offer)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host=os.environ.get("BIND", "172.17.0.1"), port=int(os.environ.get("PORT", "9150")), print=None)


if __name__ == "__main__":
    main()
