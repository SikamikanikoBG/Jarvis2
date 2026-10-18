/**
 * The line: Jarvis's voice and the hold music as the remote audio track of a real WebRTC call
 * (scripts/voiceline on ardi), instead of audio the page plays itself.
 *
 * Why (2026-10-16). Played through Web Audio, his voice was media to Android, not the far end of
 * a call: the phone's echo canceller had no reference for it, so the microphone heard him and the
 * music, and Whisper wrote them down as Arsen's words ("Абонирайте се на нашия канал!", and
 * Jarvis's own "Checking the main threads" back as a question 14 s later); where the sound came
 * out and which volume the keys turned depended on the order things were opened. A call's remote
 * track is what the phone's call stack is built for: it is the echo canceller's reference, it
 * plays on the call's route, and the volume keys are the call's.
 *
 * Nothing about the conversation moves: the session (session.ts) decides what is said and when,
 * exactly as before. The line is only where it comes out. A sentence's bytes still come from the
 * core's /api/tts; they are handed to the line over the call's data channel and the line says when
 * each has been heard out. The microphone stays the listener's (listener.ts): this connection only
 * receives.
 */

import { api } from '../api/client';
import { DeviceSpeaker, ServerSpeaker } from './speaker';
import type { Speaker } from './session';
import type { Cues } from './tones';
import { callTrace } from './trace';

/** How long the call may take to come up before the page plays the voice itself after all. */
const CONNECT_TIMEOUT_MS = 8000;
/** The data channel's messages: a sentence goes over in pieces this big. */
const CHUNK = 16 * 1024;
/** A sentence the line never reports as heard out frees the call after its length and this. */
const PLAY_SLACK_MS = 4000;

type LineEvent = { t: 'started' | 'done' | 'error'; id: string; cut?: boolean; error?: string };

export class VoiceLine {
  private pc: RTCPeerConnection | null = null;
  private dc: RTCDataChannel | null = null;
  private audio: HTMLAudioElement | null = null;
  private seq = 0;
  private playing = new Map<string, { resolve: (heard: boolean) => void; watchdog: ReturnType<typeof setTimeout> | null; seconds: number }>();
  private paused = false;
  private outputs: MediaDeviceInfo[] = [];
  /** The line is down (or never came up): the page plays the voice itself from here on. */
  down = false;
  onDown: (() => void) | null = null;
  /** Settles when the call is up or has been given up on: a sentence waits for it, not longer. */
  readonly ready: Promise<void>;
  private settle: () => void = () => undefined;
  /** The music as last asked for, sent again the moment the channel opens. */
  private wantMusic: { on: boolean; volume: number } | null = null;

  constructor() {
    this.ready = new Promise<void>((resolve) => (this.settle = resolve));
  }

  /** The call did not come up: the page plays everything itself. */
  giveUp(): void {
    this.lose();
    this.close();
  }

  /** Open the call; rejects when it does not come up in time (the caller falls back). */
  async connect(): Promise<void> {
    const pc = new RTCPeerConnection();
    this.pc = pc;
    pc.addTransceiver('audio', { direction: 'recvonly' });
    const dc = pc.createDataChannel('line');
    dc.binaryType = 'arraybuffer';
    this.dc = dc;
    dc.onmessage = (ev) => this.onEvent(ev.data);
    dc.onopen = () => {
      if (this.wantMusic) dc.send(JSON.stringify({ t: 'music', ...this.wantMusic }));
    };
    // The remote track plays through an element: the browser renders it as the call's audio.
    const audio = new Audio();
    audio.autoplay = true;
    this.audio = audio;
    pc.ontrack = (ev) => {
      audio.srcObject = ev.streams[0] ?? new MediaStream([ev.track]);
      void audio.play().catch(() => undefined);
    };
    const up = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('the call did not connect')), CONNECT_TIMEOUT_MS);
      const check = () => {
        if (pc.connectionState === 'connected' && dc.readyState === 'open') {
          clearTimeout(timer);
          resolve();
        } else if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
          clearTimeout(timer);
          reject(new Error(`the call ${pc.connectionState}`));
        }
      };
      pc.addEventListener('connectionstatechange', check);
      dc.addEventListener('open', check);
    });
    pc.addEventListener('connectionstatechange', () => {
      callTrace.mark('line.state', { state: pc.connectionState });
      if ((pc.connectionState === 'failed' || pc.connectionState === 'closed') && this.pc === pc) this.lose();
    });
    await pc.setLocalDescription(await pc.createOffer());
    // Non-trickle: the line answers once, with everything it needs.
    await new Promise<void>((resolve) => {
      if (pc.iceGatheringState === 'complete') return resolve();
      pc.addEventListener('icegatheringstatechange', () => pc.iceGatheringState === 'complete' && resolve());
      setTimeout(resolve, 2500);
    });
    const local = pc.localDescription;
    if (!local) throw new Error('no offer');
    const answer = await api.voice.lineOffer({ sdp: local.sdp, type: local.type });
    await pc.setRemoteDescription(answer);
    await up;
    this.settle();
    await this.listOutputs();
    this.report('connected');
    navigator.mediaDevices.addEventListener('devicechange', this.onDevices);
  }

  private readonly onDevices = () => void this.listOutputs().then(() => this.report('devicechange'));

  /** What only the phone knows - its outputs and inputs as the browser names them, where the
   *  call's sound is set to go - into the line's log, so a call is read from data, not guessed. */
  private report(why: string): void {
    void navigator.mediaDevices
      .enumerateDevices()
      .catch(() => [] as MediaDeviceInfo[])
      .then((devices) => {
        if (this.dc?.readyState !== 'open') return;
        const audio = this.audio;
        this.dc.send(
          JSON.stringify({
            t: 'diag',
            why,
            sink: audio?.sinkId ?? null,
            setSinkId: 'setSinkId' in HTMLMediaElement.prototype,
            devices: devices.map((d) => `${d.kind}:${d.label || d.deviceId.slice(0, 8)}`),
            ua: navigator.userAgent,
          }),
        );
      });
  }

  /** Say one sentence's audio; resolves true when it was heard out, false when it was cut. */
  play(bytes: ArrayBuffer, seconds: number): Promise<boolean> {
    const dc = this.dc;
    if (dc?.readyState !== 'open' || this.down) return Promise.resolve(false);
    const id = `s${(this.seq += 1)}`;
    return new Promise<boolean>((resolve) => {
      const watchdog = this.paused ? null : this.arm(id, seconds);
      this.playing.set(id, { resolve, watchdog, seconds });
      dc.send(JSON.stringify({ t: 'clip', id, size: bytes.byteLength }));
      for (let i = 0; i < bytes.byteLength; i += CHUNK) dc.send(bytes.slice(i, i + CHUNK));
    });
  }

  /** Hold the voice where it is; `resume` goes on from that spot. No sentence times out meanwhile. */
  pause(): void {
    this.paused = true;
    if (this.dc?.readyState === 'open') this.dc.send(JSON.stringify({ t: 'pause' }));
    for (const p of this.playing.values()) {
      if (p.watchdog) clearTimeout(p.watchdog);
      p.watchdog = null;
    }
  }

  resume(): void {
    this.paused = false;
    if (this.dc?.readyState === 'open') this.dc.send(JSON.stringify({ t: 'resume' }));
    for (const [id, p] of this.playing) p.watchdog ??= this.arm(id, p.seconds);
  }

  private arm(id: string, seconds: number): ReturnType<typeof setTimeout> {
    return setTimeout(() => this.finish(id, false), seconds * 1000 + PLAY_SLACK_MS);
  }

  /** Cut: what is playing and what is queued on the line stop now. */
  stop(): void {
    this.paused = false;
    if (this.dc?.readyState === 'open') this.dc.send(JSON.stringify({ t: 'stop' }));
    for (const id of [...this.playing.keys()]) this.finish(id, false);
  }

  music(on: boolean, volume: number): void {
    this.wantMusic = { on, volume };
    if (this.dc?.readyState === 'open') this.dc.send(JSON.stringify({ t: 'music', on, volume }));
  }

  /** The outputs the browser lets the page choose between (none on some phones). */
  get choices(): number {
    return 'setSinkId' in HTMLMediaElement.prototype ? this.outputs.length : 0;
  }

  /** Move the call's sound to the next output the browser offers; false when it offers none. */
  async nextOutput(): Promise<boolean> {
    await this.listOutputs();
    const audio = this.audio;
    if (!audio || !('setSinkId' in audio) || this.outputs.length < 2) return false;
    const at = this.outputs.findIndex((o) => o.deviceId === (audio.sinkId === '' ? 'default' : audio.sinkId));
    const next = this.outputs[(at + 1) % this.outputs.length];
    if (!next) return false;
    await audio.setSinkId(next.deviceId);
    this.report('output changed');
    return true;
  }

  /** What the line looks like from the phone right now (voice/trace.ts): how loud the voice
   *  arrives, packets lost, the jitter buffer and gaps filled in, the round trip, and whether the
   *  element playing it is playing. */
  async stats(): Promise<Record<string, unknown> | null> {
    const pc = this.pc;
    if (!pc) return null;
    const out: Record<string, unknown> = { paused: this.paused, playing: this.playing.size, el: this.audio ? { paused: this.audio.paused, muted: this.audio.muted, volume: this.audio.volume } : null };
    const report = await pc.getStats();
    report.forEach((r: RTCStats & Record<string, unknown>) => {
      if (r.type === 'inbound-rtp' && r.kind === 'audio') {
        const pick = ['audioLevel', 'totalAudioEnergy', 'packetsReceived', 'packetsLost', 'jitter', 'jitterBufferDelay', 'jitterBufferEmittedCount', 'concealedSamples', 'silentConcealedSamples', 'totalSamplesReceived'] as const;
        out.in = Object.fromEntries(pick.map((k) => [k, r[k] ?? null]));
      } else if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.nominated) {
        out.rtt = r.currentRoundTripTime ?? null;
      }
    });
    return out;
  }

  close(): void {
    navigator.mediaDevices.removeEventListener('devicechange', this.onDevices);
    this.stop();
    try {
      this.dc?.close();
      this.pc?.close();
    } catch {
      /* already closed */
    }
    if (this.audio) this.audio.srcObject = null;
    this.pc = this.dc = this.audio = null;
  }

  private async listOutputs(): Promise<void> {
    try {
      this.outputs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audiooutput');
    } catch {
      this.outputs = [];
    }
  }

  private onEvent(data: unknown): void {
    if (typeof data !== 'string') return;
    let ev: LineEvent;
    try {
      ev = JSON.parse(data) as LineEvent;
    } catch {
      return;
    }
    if (ev.t === 'done') this.finish(ev.id, !ev.cut);
  }

  private finish(id: string, heard: boolean): void {
    const p = this.playing.get(id);
    if (!p) return;
    this.playing.delete(id);
    if (p.watchdog) clearTimeout(p.watchdog);
    p.resolve(heard);
  }

  private lose(): void {
    this.settle();
    if (this.down) return;
    this.down = true;
    for (const id of [...this.playing.keys()]) this.finish(id, false);
    this.onDown?.();
  }
}

/**
 * The voice over the line: the same sentences from the same /api/tts, played as the call's
 * remote track. If the line goes down mid-call the page plays them itself (ServerSpeaker), which
 * falls back to the device voice in turn - a call goes on rather than going silent.
 */
export class LineSpeaker implements Speaker {
  private prepared = new Map<string, Promise<ArrayBuffer | null>>();
  private problem: string | null = null;
  private readonly fallback: ServerSpeaker;
  private readonly device = new DeviceSpeaker();
  private fellBack = false;

  constructor(
    private readonly line: VoiceLine,
    private readonly o: { contextOf: () => AudioContext | null; cache: boolean },
  ) {
    this.fallback = new ServerSpeaker({ contextOf: o.contextOf, cache: o.cache });
  }

  available(): boolean {
    return true;
  }

  /** The voice is the call's remote track: the phone's echo canceller has it, on every route. */
  cancellable(): boolean {
    return this.line.down ? this.fallback.cancellable() : !this.fellBack;
  }

  takeProblem(): string | null {
    const p = this.problem ?? this.fallback.takeProblem();
    this.problem = null;
    return p;
  }

  beginReply(): void {
    this.fellBack = false;
    this.fallback.beginReply();
  }

  prepare(text: string, lang: string, first = false): void {
    if (this.line.down) return this.fallback.prepare(text, lang, first);
    const key = `${lang}|${text}`;
    if (!this.prepared.has(key)) this.prepared.set(key, this.fetch(text, lang, first));
  }

  async speak(text: string, lang: string): Promise<void> {
    await this.line.ready;
    if (this.line.down) return this.fallback.speak(text, lang);
    if (this.fellBack) return this.device.speak(text, lang);
    const key = `${lang}|${text}`;
    const pending = this.prepared.get(key) ?? this.fetch(text, lang);
    this.prepared.delete(key);
    const bytes = await pending;
    if (!bytes) {
      this.fellBack = true;
      return this.device.speak(text, lang);
    }
    // The line says when it has been heard out; the length only bounds the wait if it never does.
    await this.line.play(bytes, estimateSeconds(bytes));
    if (this.line.down) this.problem = 'The call line dropped; the voice now plays from the page.';
  }

  pause(): void {
    if (!this.line.down) this.line.pause();
  }

  resume(): void {
    if (!this.line.down) this.line.resume();
  }

  cancel(): void {
    this.line.stop();
    this.prepared.clear();
    this.fallback.cancel();
    this.device.cancel();
  }

  close(): void {
    this.cancel();
    this.fallback.close();
  }

  private async fetch(text: string, lang: string, first = false): Promise<ArrayBuffer | null> {
    try {
      return await api.tts(text, lang, { cache: this.o.cache, first });
    } catch (e) {
      this.problem = `The server voice failed (${e instanceof Error ? e.message : 'error'}); using the device voice.`;
      return null;
    }
  }
}

/** A sentence's length from its bytes: the core's MP3s are 64-48 kbps; long enough is enough. */
function estimateSeconds(bytes: ArrayBuffer): number {
  return bytes.byteLength / 6000;
}

/**
 * The hold music on the line, so it reaches the phone as the call's own sound (and the echo
 * canceller removes it from the microphone like his voice). The short signals - accepted, the
 * line to Jarvis lost - are still the page's own notes (tones.ts): brief, and the microphone is
 * hushed while they sound.
 */
export class LineCues implements Cues {
  private musicOn = false;

  constructor(
    private readonly line: VoiceLine,
    private readonly inner: Cues,
    private readonly o: { waiting: 'music' | 'beeps' | 'silence'; volume: number; onHold?: (on: boolean) => void },
  ) {}

  accepted(): void {
    this.inner.accepted();
  }

  thinking(on: boolean): void {
    if (this.o.waiting !== 'music' || this.line.down) {
      if (this.musicOn) this.music(false);
      this.inner.thinking(on);
      return;
    }
    this.music(on);
  }

  offline(on: boolean): void {
    this.inner.offline(on);
  }

  close(): void {
    this.music(false);
    this.inner.close();
  }

  private music(on: boolean): void {
    if (on === this.musicOn) return;
    this.musicOn = on;
    this.line.music(on, this.o.volume);
    this.o.onHold?.(on);
  }
}
