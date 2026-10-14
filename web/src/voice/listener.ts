/**
 * The microphone as utterances: getUserMedia → raw PCM → the segmenter → one WAV per thing said,
 * handed to whoever recognises it.
 *
 * Raw samples, not MediaRecorder. A WebM stream carries its header in the first chunk only, so a
 * blob stitched from the chunks of one utterance in the middle of a recording is a file no
 * decoder opens. Here the samples sit in a ring buffer (the last minute), the segmenter says
 * where an utterance started and ended, and that slice — plus a little pre-roll, because the
 * first syllable arrives before the segmenter is sure — is written out as 16 kHz mono WAV, the
 * one format every Whisper takes without a transcode.
 *
 * A long utterance is recognised in pieces while he is still talking (docs/stories/12_assistant.md):
 * when he goes on after a pause, what came before it — once it is long enough to recognise well on
 * its own — is a piece, whose words are already on their way (the recognition started at that
 * pause). What is sent at the end is only the rest, with up to 8 s before it for the turn model,
 * which judges an ending by how the voice falls, not by the words.
 *
 * While Jarvis speaks, his own voice reaches the microphone — and echo cancellation does NOT
 * catch it, because `speechSynthesis` plays outside the browser's audio path (on the laptop
 * the answer came back as the next question). Two routes, two answers (see CallRoute):
 *
 * - earpiece: the microphone is held throughout, and an `EchoGate` learns how loud he is in it
 *   so that only a voice clearly over his counts — a cut-in, not his own sentence;
 * - speaker: the microphone is opened without the platform's echo cancelling - which is what
 *   puts Android in call mode and the sound in the earpiece - and held throughout; the same gate
 *   keeps his voice from the loudspeaker out of the utterances (see MIC_CONSTRAINTS).
 */

import { EchoGate } from './gate';
import { rmsOf, Segmenter, type SegmentEvent } from './segmenter';
import type { CallRoute, Listener, ListenerHandlers } from './session';

const TARGET_RATE = 16_000;
/** Samples kept: the last minute at 16 kHz. An utterance is capped at 30 s by the segmenter. */
const RING_SECONDS = 60;
/** Audio kept from before the segmenter called speech, so the first syllable is not lost. */
const PRE_ROLL_MS = 300;
/** A cut-in has to hold for longer than an ordinary utterance's onset: a cough over him is not a cut-in. */
const CUT_IN_ATTACK_MS = 450;
/** A pause closes a piece of a long utterance only once this much speech is behind it: Whisper
 *  recognises a few seconds with the words before it as its prompt as well as the whole. */
const PIECE_MIN_MS = 4000;
/** What the turn model is given: the last 8 s, the window it was trained on. */
const TURN_CONTEXT_MS = 8000;
/** The worklet that taps the samples; served with the SPA (web/public/pcm-worklet.js). */
const WORKLET_URL = '/pcm-worklet.js';

/**
 * The microphone per route. Echo cancelling is what puts Android in call mode, and call mode is
 * what sends every sound to the earpiece: on the speaker route it is off, the microphone stays
 * open the whole time, and the echo gate (which learns how loud he is in the room) keeps his own
 * voice from being heard as words. Releasing the microphone while he spoke was meant to leave
 * call mode, but the phone switched back too late and he stayed in the earpiece (2026-10-14).
 */
const MIC_CONSTRAINTS: Record<CallRoute, MediaStreamConstraints> = {
  earpiece: { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } },
  speaker: { audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 } },
};

export const micSupported = (): boolean =>
  typeof navigator !== 'undefined' &&
  typeof navigator.mediaDevices?.getUserMedia === 'function' &&
  typeof AudioContext !== 'undefined' &&
  typeof AudioWorkletNode !== 'undefined';

export class MicListener implements Listener {
  private stream: MediaStream | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private ctx: AudioContext | null = null;
  private tap: AudioWorkletNode | null = null;
  private seg = new Segmenter();
  private gate = new EchoGate();
  private ring = new Int16Array(TARGET_RATE * RING_SECONDS);
  /** Absolute index (in 16 kHz samples) of the next sample to be written. */
  private written = 0;
  private utteranceStartSample = 0;
  /** Where the part of this utterance that is not yet a recognised piece begins. */
  private pieceStartSample = 0;
  /** The last pause of this utterance: a breath, if he goes on after it. */
  private lastPause: { at: number; sample: number } | null = null;
  private speaking = false;
  /** Until this moment on the call's clock the microphone is ignored (a cue is sounding). */
  private suppressUntil = 0;
  private route: CallRoute = 'earpiece';
  private muted = false;
  private handlers: ListenerHandlers | null = null;
  private t0 = 0;
  private acquiring: Promise<void> | null = null;

  /** The context the microphone runs in: created inside the "start call" tap and proven running
   *  by the level ring, so the voice plays through it too rather than through a second context
   *  opened later, outside any gesture, while the phone is in call mode. */
  get audioContext(): AudioContext | null {
    return this.ctx && this.ctx.state !== 'closed' ? this.ctx : null;
  }

  async start(handlers: ListenerHandlers): Promise<void> {
    this.handlers = handlers;
    this.ctx = new AudioContext();
    if (this.ctx.state === 'suspended') await this.ctx.resume();
    await this.ctx.audioWorklet.addModule(WORKLET_URL);
    this.tap = new AudioWorkletNode(this.ctx, 'pcm-tap', { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1 });
    const rate = this.ctx.sampleRate;
    this.tap.port.onmessage = (e: MessageEvent<Float32Array>) => this.onAudio(e.data, rate);
    this.t0 = performance.now();
    await this.acquire();
  }

  stop(): void {
    this.release();
    this.tap?.port.close();
    this.tap?.disconnect();
    this.tap = null;
    void this.ctx?.close();
    this.ctx = null;
    this.seg = new Segmenter();
    this.gate = new EchoGate();
    this.written = 0;
  }

  /** Take the microphone (again). Idempotent; a second call while one is in flight joins it. */
  private acquire(): Promise<void> {
    if (this.stream) return Promise.resolve();
    this.acquiring ??= (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS[this.route]);
        if (!this.ctx || !this.tap) {
          stream.getTracks().forEach((t) => t.stop()); // stopped while we were asking
          return;
        }
        this.stream = stream;
        this.source = this.ctx.createMediaStreamSource(stream);
        this.source.connect(this.tap);
        // A fresh microphone is a fresh room: the floor warms up again before anything counts.
        this.seg = new Segmenter(this.seg.opts);
      } finally {
        this.acquiring = null;
      }
    })();
    return this.acquiring;
  }

  private release(): void {
    this.source?.disconnect();
    this.source = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  setSpeaking(on: boolean, echoCancelled = false): void {
    this.speaking = on;
    const now = performance.now() - this.t0;
    // On the speaker nothing subtracts his voice from the microphone, whatever plays it.
    if (on) this.gate.setProfile(echoCancelled && this.route === 'earpiece');
    this.gate.setSpeaking(on, now, this.seg.noiseFloor);
    this.seg.setAttack(on ? CUT_IN_ATTACK_MS : null);
  }

  /** Hold music is playing (tones.ts): judged like his voice — the page's own audio, which the
   *  echo canceller subtracts, with a cut-in needing to be clearly over it. Unlike his voice the
   *  microphone is never let go for it, on either route: it is background, he may talk over it. */
  setHold(on: boolean): void {
    if (this.speaking) return; // his voice already decides the gate
    const now = performance.now() - this.t0;
    if (on) this.gate.setProfile(this.route === 'earpiece');
    this.gate.setSpeaking(on, now, this.seg.noiseFloor);
    this.seg.setAttack(on ? CUT_IN_ATTACK_MS : null);
  }

  setRoute(route: CallRoute): void {
    if (route === this.route) return;
    this.route = route;
    // The route is the microphone's settings: reopen it with the new ones (if it is open yet).
    if (!this.ctx) return;
    this.release();
    void this.acquire().catch((e: unknown) => this.handlers?.onError(e instanceof Error ? e.message : 'The microphone could not be reopened.'));
    if (this.speaking) this.setSpeaking(true);
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    if (muted) this.seg = new Segmenter(this.seg.opts);
  }

  /** The turn model is sure he finished at the pause `at`: close the utterance now instead of
   *  waiting out the release. False when he has spoken since — then it was a breath. */
  finish(at: number): boolean {
    const t = performance.now() - this.t0;
    const events = this.seg.endAt(at);
    for (const ev of events) this.onSegment(ev, t);
    return events.length > 0;
  }

  /** Ignore the microphone for a moment: a cue is sounding, and a beep is not a word. */
  suppress(ms: number): void {
    this.suppressUntil = Math.max(this.suppressUntil, performance.now() - this.t0 + ms);
  }

  private onAudio(input: Float32Array, rate: number): void {
    if (!this.handlers) return;
    // Level from the raw frame (before the mute zeroes it), for the ring on screen.
    const rms = this.muted ? 0 : rmsFloat(input);
    this.handlers.onLevel(Math.min(1, rms * 4));
    // Downsample to 16 kHz by picking every n-th sample (the mic is band-limited well under
    // 8 kHz by the browser's own processing; a proper filter here buys nothing audible).
    const step = rate / TARGET_RATE;
    for (let i = 0; i < input.length; i += step) {
      const v = this.muted ? 0 : Math.max(-1, Math.min(1, input[Math.floor(i)] ?? 0));
      this.ring[this.written % this.ring.length] = v < 0 ? v * 0x8000 : v * 0x7fff;
      this.written += 1;
    }
    const t = performance.now() - this.t0;
    // Through the gate: while he speaks, only a voice clearly over his own reaches the segmenter.
    const level = t < this.suppressUntil ? 0 : this.gate.pass(rms, t, this.seg.noiseFloor);
    for (const ev of this.seg.push(level, t)) this.onSegment(ev, t);
  }

  private onSegment(ev: SegmentEvent, t: number): void {
    if (!this.handlers) return;
    if (ev.type === 'speech-start') {
      const preRoll = Math.round((PRE_ROLL_MS / 1000) * TARGET_RATE);
      const startedAgoMs = t - ev.at;
      this.utteranceStartSample = Math.max(0, this.written - Math.round((startedAgoMs / 1000) * TARGET_RATE) - preRoll);
      this.pieceStartSample = this.utteranceStartSample;
      this.lastPause = null;
      // 450 ms of voice over his own is a decision, not a guess: the rest of it comes through
      // whole, or his sentence arrives as fragments too short to be an utterance.
      if (this.speaking) this.gate.latch();
      this.handlers.onSpeechStart();
    } else if (ev.type === 'dropped') {
      // He stopped him and then said nothing a recogniser could use: a cough, a chair.
      this.gate.unlatch();
      this.handlers.onSpeechDropped();
    } else if (ev.type === 'pause') {
      this.closePiece(ev.at);
      this.lastPause = { at: ev.at, sample: this.written };
      // Maybe the end: the words so far go to be recognised now, while the release runs.
      const tail = this.tail(this.written);
      if (tail) this.handlers.onPause(tail.audio, ev.at, tail.offsetMs);
    } else if (ev.type === 'speech-end') {
      this.gate.unlatch();
      this.closePiece(ev.at);
      const tail = this.tail(this.written);
      if (tail) this.handlers.onUtterance(tail.audio, ev.at, tail.offsetMs);
      else this.handlers.onSpeechDropped();
    }
  }

  /** He went on after the last pause: if enough came before it, that much is a piece now — if
   *  the session has its words on the way (it started recognising them at that pause). */
  private closePiece(at: number): void {
    const last = this.lastPause;
    if (!last || last.at === at || !this.handlers) return;
    const minSamples = (PIECE_MIN_MS / 1000) * TARGET_RATE;
    if (last.sample - this.pieceStartSample >= minSamples && this.handlers.onPiece?.(last.at)) {
      this.pieceStartSample = last.sample;
    }
  }

  /** The part of the utterance not yet recognised, with up to 8 s before it for the turn model;
   *  `offsetMs` is where the unrecognised part begins within the audio. */
  private tail(end: number): { audio: Blob; offsetMs: number } | null {
    const context = Math.round((TURN_CONTEXT_MS / 1000) * TARGET_RATE);
    const from = Math.max(this.utteranceStartSample, Math.min(this.pieceStartSample, end - context));
    const audio = this.slice(from, end);
    return audio ? { audio, offsetMs: ((this.pieceStartSample - from) / TARGET_RATE) * 1000 } : null;
  }

  private slice(from: number, to: number): Blob | null {
    const n = to - from;
    if (n <= 0 || n > this.ring.length) return null;
    const out = new Int16Array(n);
    for (let i = 0; i < n; i++) out[i] = this.ring[(from + i) % this.ring.length] ?? 0;
    return wavBlob(out, TARGET_RATE);
  }
}

function rmsFloat(frame: Float32Array): number {
  let sum = 0;
  for (const v of frame) sum += v * v;
  return Math.sqrt(sum / Math.max(1, frame.length));
}

/** 16-bit mono PCM as a WAV file. 44 bytes of header, then the samples as they are. */
export function wavBlob(samples: Int16Array, rate: number): Blob {
  const header = new ArrayBuffer(44);
  const v = new DataView(header);
  const write = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i));
  };
  write(0, 'RIFF');
  v.setUint32(4, 36 + samples.length * 2, true);
  write(8, 'WAVE');
  write(12, 'fmt ');
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  write(36, 'data');
  v.setUint32(40, samples.length * 2, true);
  return new Blob([header, samples.buffer as ArrayBuffer], { type: 'audio/wav' });
}

export { rmsOf };
