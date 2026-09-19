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
 *
 * Where it plays: through the microphone's own AudioContext when the listener lends one
 * (`contextOf`). That context was opened inside the "start call" tap and is proven running by
 * the level ring; a second context opened later — from a WebSocket delta, with the phone
 * already in call mode — is the one that can come up suspended, and a source started on a
 * suspended context never ends: the call sat on "Speaking" in silence (2026-09-13, "the tts
 * in the chat call is not working"). Belt and braces for that: `resume()` is awaited with a
 * deadline, a context that will not run hands the sentence to the device voice, every play
 * has a watchdog a little longer than the clip, and the reason reaches the screen.
 */
export class ServerSpeaker implements Speaker {
  private own: AudioContext | null = null;
  private current: { source: AudioBufferSourceNode; resolve: () => void; watchdog: ReturnType<typeof setTimeout> } | null = null;
  private prepared = new Map<string, Promise<AudioBuffer | null>>();
  private readonly fallback = new DeviceSpeaker();
  private problem: string | null = null;
  /** Set once the server has failed, so the screen can say the voice is the device's. */
  fellBack = false;

  constructor(private readonly o: { contextOf?: () => AudioContext | null; cache?: boolean } = {}) {}

  available(): boolean {
    return typeof AudioContext !== 'undefined';
  }

  /** The page plays these bytes itself, so the browser's echo canceller subtracts them from the
   *  microphone — unless this voice has fallen back to the device's, which it does not. */
  cancellable(): boolean {
    return !this.fellBack;
  }

  takeProblem(): string | null {
    const p = this.problem;
    this.problem = null;
    return p;
  }

  /** Start fetching a sentence now; `speak` will find it ready. */
  prepare(text: string, lang: string): void {
    const key = `${lang}|${text}`;
    if (this.prepared.has(key)) return;
    this.prepared.set(key, this.fetch(text, lang));
  }

  private context(): AudioContext {
    const lent = this.o.contextOf?.();
    if (lent) return lent;
    if (!this.own || this.own.state === 'closed') this.own = new AudioContext();
    return this.own;
  }

  /** True once the context runs; false when it will not within the deadline. */
  private async running(ctx: AudioContext, deadlineMs = 1500): Promise<boolean> {
    if (ctx.state === 'running') return true;
    const resumed = ctx.resume().then(() => true, () => false);
    const late = new Promise<boolean>((r) => setTimeout(() => r(false), deadlineMs));
    // Re-read after the await: TypeScript's narrowing does not know a state can change.
    return (await Promise.race([resumed, late])) && (ctx.state as AudioContextState) === 'running';
  }

  private async fetch(text: string, lang: string): Promise<AudioBuffer | null> {
    try {
      const bytes = await api.tts(text, lang, { cache: this.o.cache ?? true });
      return await this.context().decodeAudioData(bytes);
    } catch (e) {
      this.problem = `The server voice failed (${e instanceof Error ? e.message : 'error'}); using the device voice.`;
      return null;
    }
  }

  /** A new reply: the server voice gets another chance after a fallback. Within one reply the
   *  voice never changes — once a sentence had to be the device's, the rest of that reply is too,
   *  rather than the two voices taking turns (2026-09-16). */
  beginReply(): void {
    this.fellBack = false;
  }

  async speak(text: string, lang: string): Promise<void> {
    if (this.fellBack) return this.fallback.speak(text, lang);
    const key = `${lang}|${text}`;
    const pending = this.prepared.get(key) ?? this.fetch(text, lang);
    this.prepared.delete(key);
    const buffer = await pending;
    if (!buffer) {
      this.fellBack = true;
      return this.fallback.speak(text, lang);
    }
    const ctx = this.context();
    if (!(await this.running(ctx))) {
      this.fellBack = true;
      this.problem = `The browser would not play the voice (audio ${ctx.state}); using the device voice.`;
      return this.fallback.speak(text, lang);
    }
    return new Promise<void>((resolve) => {
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        if (this.current?.source === source) {
          clearTimeout(this.current.watchdog);
          this.current = null;
        }
        resolve();
      };
      source.onended = finish;
      // A source that never ends (a context that went quiet under it) must not hold the call
      // on "Speaking" for ever: the clip's own length plus a little is as long as it gets.
      const watchdog = setTimeout(() => {
        if (done) return;
        this.problem = 'The voice stopped playing mid-sentence.';
        finish();
      }, buffer.duration * 1000 + 1500);
      this.current = { source, resolve: finish, watchdog };
      source.start();
    });
  }

  cancel(): void {
    const cur = this.current;
    this.current = null;
    if (cur) {
      clearTimeout(cur.watchdog);
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
    // Only a context of our own is ours to close; a lent one belongs to the microphone.
    void this.own?.close();
    this.own = null;
  }
}
