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
 * Gating: while Jarvis speaks, his own voice reaches the microphone through the loudspeaker (a
 * headset makes this moot). Echo cancellation takes most of it; what is left is judged against a
 * much higher bar, so only a clear voice over his counts as a cut-in.
 */

import { rmsOf, Segmenter, type SegmentEvent } from './segmenter';
import type { Listener, ListenerHandlers } from './session';

const TARGET_RATE = 16_000;
/** Samples kept: the last minute at 16 kHz. An utterance is capped at 30 s by the segmenter. */
const RING_SECONDS = 60;
/** Audio kept from before the segmenter called speech, so the first syllable is not lost. */
const PRE_ROLL_MS = 300;
/** While gated (Jarvis speaking), speech must clear the floor by this much more to count. */
const GATED_RATIO = 5;
/** The worklet that taps the samples; served with the SPA (web/public/pcm-worklet.js). */
const WORKLET_URL = '/pcm-worklet.js';

export const micSupported = (): boolean =>
  typeof navigator !== 'undefined' &&
  typeof navigator.mediaDevices?.getUserMedia === 'function' &&
  typeof AudioContext !== 'undefined' &&
  typeof AudioWorkletNode !== 'undefined';

export class MicListener implements Listener {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private tap: AudioWorkletNode | null = null;
  private seg = new Segmenter();
  private ring = new Int16Array(TARGET_RATE * RING_SECONDS);
  /** Absolute index (in 16 kHz samples) of the next sample to be written. */
  private written = 0;
  private utteranceStartSample = 0;
  private gated = false;
  private muted = false;
  private handlers: ListenerHandlers | null = null;
  private t0 = 0;

  async start(handlers: ListenerHandlers): Promise<void> {
    this.handlers = handlers;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.ctx = new AudioContext();
    if (this.ctx.state === 'suspended') await this.ctx.resume();
    await this.ctx.audioWorklet.addModule(WORKLET_URL);
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.tap = new AudioWorkletNode(this.ctx, 'pcm-tap', { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1 });
    const rate = this.ctx.sampleRate;
    this.tap.port.onmessage = (e: MessageEvent<Float32Array>) => this.onAudio(e.data, rate);
    source.connect(this.tap);
    this.t0 = performance.now();
  }

  stop(): void {
    this.tap?.port.close();
    this.tap?.disconnect();
    this.tap = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    void this.ctx?.close();
    this.ctx = null;
    this.seg = new Segmenter();
    this.written = 0;
  }

  setGated(gated: boolean): void {
    this.gated = gated;
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    if (muted) this.seg = new Segmenter(this.seg.opts);
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
    // Gated: the same segmenter, a much higher bar. A cut-in has to be a voice over his.
    const level = this.gated && rms < this.seg.noiseFloor * GATED_RATIO ? 0 : rms;
    for (const ev of this.seg.push(level, t)) this.onSegment(ev, t);
  }

  private onSegment(ev: SegmentEvent, t: number): void {
    if (!this.handlers) return;
    if (ev.type === 'speech-start') {
      const preRoll = Math.round((PRE_ROLL_MS / 1000) * TARGET_RATE);
      const startedAgoMs = t - ev.at;
      this.utteranceStartSample = Math.max(0, this.written - Math.round((startedAgoMs / 1000) * TARGET_RATE) - preRoll);
      this.handlers.onSpeechStart();
    } else if (ev.type === 'speech-end') {
      const endSample = this.written;
      const audio = this.slice(this.utteranceStartSample, endSample);
      if (audio) this.handlers.onUtterance(audio);
    }
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
