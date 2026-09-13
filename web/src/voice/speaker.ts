/**
 * The device's own voice: `speechSynthesis`, one sentence per utterance.
 *
 * Its edges, designed around rather than discovered on the phone: Chrome stops an utterance
 * that runs past ~15 s (so we never hand it more than a sentence); some builds never fire
 * `end` for a cancelled utterance (so a cancel resolves the promise itself); the voice list
 * loads late on Android (so it is read at speak time, not at construction); and the first
 * utterance must follow a user gesture on iOS (the "start call" tap is that gesture — the
 * session's `start()` is called from it).
 *
 * The `Speaker` interface is what the session talks to; a server-side voice (Piper, Kokoro on
 * ardi) is another implementation of the same three methods.
 */

import { api } from '../api/client';
import type { Speaker } from './session';

export const ttsSupported = (): boolean => typeof speechSynthesis !== 'undefined' && typeof SpeechSynthesisUtterance !== 'undefined';

/** The best voice for a language: a local one over a remote one, a "natural"/"neural" one over the rest. */
export function pickVoice(voices: SpeechSynthesisVoice[], lang: string): SpeechSynthesisVoice | null {
  const want = lang.toLowerCase();
  const matches = voices.filter((v) => v.lang.toLowerCase().replace('_', '-').startsWith(want));
  if (matches.length === 0) return null;
  const score = (v: SpeechSynthesisVoice) =>
    (v.localService ? 2 : 0) + (/natural|neural|premium|enhanced/i.test(v.name) ? 3 : 0) + (v.default ? 1 : 0);
  return [...matches].sort((a, b) => score(b) - score(a))[0] ?? null;
}

export class DeviceSpeaker implements Speaker {
  private current: { utterance: SpeechSynthesisUtterance; resolve: () => void } | null = null;
  private rate: number;

  constructor(rate = 1.0) {
    this.rate = rate;
  }

  available(): boolean {
    return ttsSupported();
  }

  /** Whether a voice exists for the language at all — the call screen says so before the first turn. */
  hasVoiceFor(lang: string): boolean {
    return ttsSupported() && pickVoice(speechSynthesis.getVoices(), lang) !== null;
  }

  speak(text: string, lang: string): Promise<void> {
    if (!ttsSupported()) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const u = new SpeechSynthesisUtterance(text);
      const voice = pickVoice(speechSynthesis.getVoices(), lang);
      if (voice) u.voice = voice;
      u.lang = voice?.lang ?? lang;
      u.rate = this.rate;
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        if (this.current?.utterance === u) this.current = null;
        resolve();
      };
      u.onend = finish;
      u.onerror = finish;
      this.current = { utterance: u, resolve: finish };
      // A stale paused state (a previous call ended mid-word) keeps everything silent.
      if (speechSynthesis.paused) speechSynthesis.resume();
      speechSynthesis.speak(u);
    });
  }

  cancel(): void {
    if (!ttsSupported()) return;
    const cur = this.current;
    speechSynthesis.cancel();
    // Not every engine fires `end` (or anything) for a cancelled utterance.
    cur?.resolve();
    this.current = null;
  }
}

/**
 * The core's voice: each sentence fetched as MP3 from `/api/tts` and played through WebAudio.
 *
 * Why the page plays it rather than an <audio> element or the device engine: audio the page
 * plays is audio the browser's echo canceller knows about, so the microphone stops hearing him
 * (the laptop bug); and on Android it stays on the call's route instead of the TTS engine's.
 * The next sentence is fetched while the current one plays (`prepare`), so the gap between
 * sentences is the network only once, at the first. When the core cannot synthesise, the
 * device voice says that sentence instead — and the call goes on.
 */
export class ServerSpeaker implements Speaker {
  private ctx: AudioContext | null = null;
  private current: { source: AudioBufferSourceNode; resolve: () => void } | null = null;
  private prepared = new Map<string, Promise<AudioBuffer | null>>();
  private readonly fallback = new DeviceSpeaker();
  /** Set once the server has failed, so the screen can say the voice is the device's. */
  fellBack = false;

  available(): boolean {
    return typeof AudioContext !== 'undefined';
  }

  /** Start fetching a sentence now; `speak` will find it ready. */
  prepare(text: string, lang: string): void {
    const key = `${lang}|${text}`;
    if (this.prepared.has(key)) return;
    this.prepared.set(key, this.fetch(text, lang));
  }

  private context(): AudioContext {
    this.ctx ??= new AudioContext();
    if (this.ctx.state === 'suspended') void this.ctx.resume();
    return this.ctx;
  }

  private async fetch(text: string, lang: string): Promise<AudioBuffer | null> {
    try {
      const bytes = await api.tts(text, lang);
      return await this.context().decodeAudioData(bytes);
    } catch {
      return null;
    }
  }

  async speak(text: string, lang: string): Promise<void> {
    const key = `${lang}|${text}`;
    const pending = this.prepared.get(key) ?? this.fetch(text, lang);
    this.prepared.delete(key);
    const buffer = await pending;
    if (!buffer) {
      this.fellBack = true;
      return this.fallback.speak(text, lang);
    }
    const ctx = this.context();
    return new Promise<void>((resolve) => {
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        if (this.current?.source === source) this.current = null;
        resolve();
      };
      source.onended = finish;
      this.current = { source, resolve: finish };
      source.start();
    });
  }

  cancel(): void {
    const cur = this.current;
    this.current = null;
    if (cur) {
      try {
        cur.source.stop();
      } catch {
        /* already ended */
      }
      cur.resolve();
    }
    this.prepared.clear();
    this.fallback.cancel();
  }

  close(): void {
    this.cancel();
    void this.ctx?.close();
    this.ctx = null;
  }
}
