"""Step 1 prototype: a Jarvis voice call as a real WebRTC call (Pipecat + SmallWebRTC).

Not production and not wired to Jarvis's brain: it proves the audio path on a phone. Jarvis's
voice and the hold music are the call's remote audio track (mixed here, in the bot), so the
browser's echo canceller has them as its reference and Android treats the page as a call.

    phone ── WebRTC (Opus, AEC/NS/AGC in the browser) ──► SmallWebRTC transport
      Silero VAD + smart-turn v3 (Pipecat's turn-taking, barge-in)
      → Whisper (ardi, /asr) → qwen3.8-27b (vLLM) → OmniVoice "ryan" (ardi's 3090)
      → hold music mixed into the same output track while the LLM works

Every call writes traces/<call>.jsonl: the browser's track settings and audio route, every
transcript, every spoken sentence, barge-ins, hold music on/off. Run:

    uv run python bot.py            # 127.0.0.1:9150, published with tailscale serve on :8446
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
import wave
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import soundfile as sf
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse
from loguru import logger
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    MixerEnableFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.time import time_now_iso8601

HERE = Path(__file__).parent
TRACES = Path(os.environ.get("CALL_TRACES", HERE / "traces"))
HOLD_WAV = HERE / "assets" / "hold.wav"

# The services Jarvis itself uses on ardi (core settings: stt_url/stt_kind, voice.omnivoice_*,
# the chat role's endpoint). Read from the environment so nothing here is tied to one box.
STT_URL = os.environ.get("CALL_STT_URL", "http://100.97.120.53:9110")
TTS_URL = os.environ.get("CALL_TTS_URL", "http://100.97.120.53:9120")
TTS_VOICE = os.environ.get("CALL_TTS_VOICE", "ryan")
TTS_STEPS = int(os.environ.get("CALL_TTS_STEPS", "16"))
LLM_URL = os.environ.get("CALL_LLM_URL", "http://100.76.27.18:18022/v1")
LLM_MODEL = os.environ.get("CALL_LLM_MODEL", "qwen3.8-27b")
# Qwen thinks before it answers (vLLM keeps the reasoning out of the spoken text): a hard question
# then waits as long as a real Jarvis turn with tools does, which is what the hold music is for.
LLM_THINK = os.environ.get("CALL_LLM_THINK", "1") == "1"
LANGUAGE = "en"
OUT_RATE = 24_000  # OmniVoice's native rate; the mixer's loop is rendered at it too

SYSTEM = (
    "You are Jarvis, Arsen's personal assistant, and you are on a phone call with him. "
    "Speak English. What you say is read aloud by a speech synthesiser: answer like a person on "
    "the phone, in short spoken sentences, two or three unless he asks for more; if he asks for a "
    "story or a long explanation, give it. No markdown, no lists, no headings, no code, no links. "
    "This is a test call of a new audio line: you can talk about anything, but you have no tools, "
    "so if he asks you to send, open or change something, say you can't from this test line."
)


# --- the trace ----------------------------------------------------------------------------------


class Trace:
    """One JSONL file per call: what happened, when, from both ends of the line."""

    def __init__(self, call_id: str) -> None:
        TRACES.mkdir(parents=True, exist_ok=True)
        self.call_id = call_id
        self.path = TRACES / f"{datetime.now():%Y%m%d-%H%M%S}-{call_id}.jsonl"
        self._t0 = time.monotonic()
        self._file = self.path.open("a", encoding="utf-8")
        self.on_event: Any = None  # mirrored to the page's diagnostics panel

    def write(self, kind: str, **data: Any) -> None:
        row = {
            "t": round(time.monotonic() - self._t0, 3),
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            **data,
        }
        self._file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._file.flush()
        if self.on_event and not kind.startswith("client."):
            try:
                self.on_event(row)
            except Exception:  # the panel is a convenience; the file is the record
                pass

    def close(self) -> None:
        self._file.close()


class TraceObserver(BaseObserver):
    """Writes the pipeline's own account of the call: turns, transcripts, sentences, barge-ins.
    Each frame is recorded once, where it is first pushed."""

    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace
        self._seen: set[int] = set()
        self._bot_speaking = False

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        # A broadcast frame is two frames, one each way; the downstream one is the record.
        if frame.id in self._seen or data.direction != FrameDirection.DOWNSTREAM:
            return
        w = self._trace.write
        match frame:
            case VADUserStartedSpeakingFrame():
                w("vad.start")
            case VADUserStoppedSpeakingFrame():
                w("vad.stop")
            case UserStartedSpeakingFrame():
                w("user.turn_start", bot_speaking=self._bot_speaking)
            case UserStoppedSpeakingFrame():
                w("user.turn_stop")
            case InterruptionFrame():
                w("barge_in" if self._bot_speaking else "interruption", bot_speaking=self._bot_speaking)
            case TranscriptionFrame():
                w("user.transcript", text=frame.text, language=str(frame.language or ""))
            case LLMFullResponseStartFrame():
                w("llm.start")
            case LLMFullResponseEndFrame():
                w("llm.end")
            case TTSTextFrame():
                w("bot.sentence", text=frame.text)
            case BotStartedSpeakingFrame():
                self._bot_speaking = True
                w("bot.speaking_start")
            case BotStoppedSpeakingFrame():
                self._bot_speaking = False
                w("bot.speaking_stop")
            case ErrorFrame():
                w("error", error=str(frame.error))
            case _:
                return
        self._seen.add(frame.id)
        if len(self._seen) > 50_000:
            self._seen.clear()


# --- services -----------------------------------------------------------------------------------


class WhisperAsrSTT(SegmentedSTTService):
    """Jarvis's Whisper (WhisperX behind ardi's nginx proxy, the ``/asr`` API the core calls with
    ``stt_kind = "asr"``). One VAD segment in, one transcript out. Nothing is filtered: what
    Whisper hears is what the bot gets, so the trace shows whether the line itself is clean."""

    def __init__(self, url: str, language: str, **kwargs: Any) -> None:
        super().__init__(settings=STTSettings(model=None, language=None), **kwargs)
        self._url = url.rstrip("/") + "/asr"
        self._language = language
        self._client = httpx.AsyncClient(timeout=30)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        params = {"output": "json", "task": "transcribe", "language": self._language}
        try:
            res = await self._client.post(self._url, params=params, files={"audio_file": ("turn.wav", audio, "audio/wav")})
            res.raise_for_status()
            text = str(res.json().get("text") or "").strip()
        except Exception as exc:
            yield ErrorFrame(error=f"whisper: {type(exc).__name__}: {exc}")
            return
        if text:
            # A segment's transcript is final: nothing more will come for that audio. Saying so
            # lets Pipecat's turn-stop strategy wait for the last segment's words instead of
            # ending the turn on the previous segment's.
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), finalized=True)
        else:
            yield None


class OmniVoiceTTS(TTSService):
    """Jarvis's own voice (scripts/omnivoice on ardi's 3090), one sentence at a time, as 24 kHz
    PCM straight onto the call's track."""

    def __init__(self, url: str, voice: str, language: str, steps: int, **kwargs: Any) -> None:
        super().__init__(
            sample_rate=OUT_RATE,
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(model=None, voice=voice, language=None),
            **kwargs,
        )
        self._url = url.rstrip("/") + "/tts"
        self._voice = voice
        self._language = language
        self._steps = steps
        self._client = httpx.AsyncClient(timeout=30)

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        body = {"text": text, "language": self._language, "voice": self._voice, "steps": self._steps, "format": "wav"}
        try:
            res = await self._client.post(self._url, json=body)
            res.raise_for_status()
            pcm, rate = sf.read(io.BytesIO(res.content), dtype="int16")
        except Exception as exc:
            yield ErrorFrame(error=f"omnivoice: {type(exc).__name__}: {exc}")
            return
        await self.stop_ttfb_metrics()
        if rate != OUT_RATE:
            yield ErrorFrame(error=f"omnivoice returned {rate} Hz, expected {OUT_RATE}")
            return
        data = np.ascontiguousarray(pcm).tobytes()
        step = OUT_RATE * 2 // 10  # 100 ms
        for i in range(0, len(data), step):
            yield TTSAudioRawFrame(data[i : i + step], OUT_RATE, 1, context_id=context_id)


# --- hold music ---------------------------------------------------------------------------------


class FadingMixer(SoundfileMixer):
    """Pipecat's soundfile mixer with the page's fades (hold.ts: 1.6 s in, 0.35 s out, exponential),
    so the music swells in rather than starting at full level, and a turn that turns out not to be
    over (the user goes on after a pause) leaves almost nothing audible behind."""

    FADE_IN_S = 1.6
    FADE_OUT_S = 0.35
    FLOOR = 1e-4  # -80 dB: where hold.ts's exponential ramps start and end

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(mixing=True, **kwargs)
        self._target = 0.0
        self._gain = self.FLOOR

    async def process_frame(self, frame: Any) -> None:
        if isinstance(frame, MixerEnableFrame):
            self._target = 1.0 if frame.enable else 0.0
            return
        await super().process_frame(frame)

    async def mix(self, audio: bytes) -> bytes:
        if self._target == 0.0 and self._gain <= self.FLOOR:
            return audio
        n = len(audio) // 2
        secs = n / self._sample_rate
        fade = self.FADE_IN_S if self._target > 0 else self.FADE_OUT_S
        goal = max(self._target, self.FLOOR)
        # Exponential ramp: the gain multiplies by (1/FLOOR) over a full fade.
        step = (1 / self.FLOOR) ** (secs / fade)
        end = min(goal, self._gain * step) if goal > self._gain else max(goal, self._gain / step)
        ramp = np.geomspace(self._gain, end, n)
        self._gain = end
        sound = self._sounds.get(self._current_sound)
        if sound is None:
            return audio
        if self._sound_pos + n > len(sound):
            self._sound_pos = 0
        chunk = sound[self._sound_pos : self._sound_pos + n] * ramp * self._volume
        self._sound_pos += n
        mixed = np.frombuffer(audio, dtype=np.int16) + chunk
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()


class HoldMusic:
    """The mixer's loop, on while Jarvis is working on an answer and off the moment he speaks or
    Arsen does. The mixer belongs to the output transport, so the music goes out on the call's own
    track. It is switched directly rather than with a MixerEnableFrame, which would wait in the
    output queue behind the reply's audio.

    On: an LLM response has started (a turn was taken and is being answered).
    Off: the reply's first audio is synthesised, the user starts speaking (barge-in, interruption),
    or the response ends without any text to say."""

    def __init__(self, mixer: FadingMixer, trace: Trace) -> None:
        self._mixer = mixer
        self._trace = trace
        self._on = False

    async def set(self, on: bool, why: str) -> None:
        if on == self._on:
            return
        self._on = on
        self._trace.write("hold.on" if on else "hold.off", why=why)
        await self._mixer.process_frame(MixerEnableFrame(on))


class HoldBeforeTTS(FrameProcessor):
    """Between the LLM and the TTS: sees the response start, its text, its end, and the user."""

    def __init__(self, hold: HoldMusic) -> None:
        super().__init__()
        self._hold = hold
        self._said = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        match frame:
            case LLMFullResponseStartFrame():
                self._said = False
                await self._hold.set(True, "llm working")
            case LLMTextFrame() if frame.text.strip():
                self._said = True
            case LLMFullResponseEndFrame() if not self._said:
                await self._hold.set(False, "llm done, nothing to say")
            case UserStartedSpeakingFrame() | InterruptionFrame():
                await self._hold.set(False, "user speaking")
        await self.push_frame(frame, direction)


class HoldAfterTTS(FrameProcessor):
    """After the TTS: the reply's first audio ends the music."""

    def __init__(self, hold: HoldMusic) -> None:
        super().__init__()
        self._hold = hold

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            await self._hold.set(False, "reply audio")
        await self.push_frame(frame, direction)


# --- one call -----------------------------------------------------------------------------------


async def run_call(connection: SmallWebRTCConnection, call_id: str) -> None:
    trace = Trace(call_id)
    trace.write(
        "call.start",
        stt=STT_URL,
        tts=f"{TTS_URL} voice={TTS_VOICE} steps={TTS_STEPS}",
        llm=f"{LLM_URL} {LLM_MODEL} thinking={LLM_THINK}",
        pc_id=connection.pc_id,
    )
    logger.info(f"call {call_id}: trace {trace.path}")

    def mirror(row: dict) -> None:
        connection.send_app_message({"type": "trace", "row": row})

    trace.on_event = mirror

    mixer = FadingMixer(sound_files={"hold": str(HOLD_WAV)}, default_sound="hold", volume=0.35)
    hold = HoldMusic(mixer, trace)
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=OUT_RATE,
            audio_out_mixer=mixer,
        ),
    )

    stt = WhisperAsrSTT(STT_URL, LANGUAGE)
    llm = OpenAILLMService(
        api_key="none",
        base_url=LLM_URL,
        settings=OpenAILLMService.Settings(
            model=LLM_MODEL,
            system_instruction=SYSTEM,
            temperature=0.6,
            max_tokens=4000,  # reasoning included
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": LLM_THINK}}},
        ),
    )
    tts = OmniVoiceTTS(TTS_URL, TTS_VOICE, LANGUAGE, TTS_STEPS)

    context = LLMContext()
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            HoldBeforeTTS(hold),
            tts,
            HoldAfterTTS(hold),
            transport.output(),
            aggregators.assistant(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        observers=[TraceObserver(trace)],
        enable_rtvi=False,
        idle_timeout_secs=600,
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport: Any, _client: Any) -> None:
        trace.write("call.connected")
        context.add_message({"role": "user", "content": "(Arsen has just called you. Greet him in one short sentence.)"})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport: Any, _client: Any) -> None:
        trace.write("call.disconnected")
        await task.cancel()

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport: Any, message: Any, _sender: Any = None) -> None:
        # What only the browser knows: track settings, route and device changes, its own stats.
        if isinstance(message, dict) and message.get("type") == "diag":
            trace.write("client." + str(message.get("event", "event")), **(message.get("data") or {}))

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        trace.write("call.end")
        trace.close()
        logger.info(f"call {call_id}: ended")


# --- the web side -------------------------------------------------------------------------------

app = FastAPI()
handler = SmallWebRTCRequestHandler()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.post("/api/offer")
async def offer(request: Request, background: BackgroundTasks) -> dict:
    body = await request.json()

    async def started(connection: SmallWebRTCConnection) -> None:
        background.add_task(run_call, connection, uuid.uuid4().hex[:8])

    answer = await handler.handle_web_request(SmallWebRTCRequest.from_dict(body), started)
    return answer or {}


@app.patch("/api/offer")
async def ice(request: Request) -> dict:
    await handler.handle_patch_request(SmallWebRTCPatchRequest(**await request.json()))
    return {"ok": True}


if __name__ == "__main__":
    if not HOLD_WAV.exists():
        raise SystemExit(f"{HOLD_WAV} missing: uv run python render_hold.py {HOLD_WAV}")
    with wave.open(str(HOLD_WAV)) as w:
        assert w.getframerate() == OUT_RATE, "hold.wav must be rendered at the output rate"
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CALL_PORT", "9150")))
