import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MicListener } from './listener';
import type { ListenerHandlers } from './session';

/**
 * The listener on a fake microphone: the worklet's frames are pushed by hand at 16 kHz, and the
 * clock moves with them, so minutes of talk run in milliseconds.
 */

const RATE = 16_000;
const FRAME_MS = 20;
const WAV_HEADER = 44;
/** The longest audio a single request may carry: a 20 s piece plus the 8 s of context. */
const MAX_REQUEST_BYTES = WAV_HEADER + 28 * RATE * 2;

const noop = (): void => undefined;

let now = 0;
type FakeTap = { port: { onmessage: ((e: { data: Float32Array }) => void) | null; close(): void }; disconnect(): void };
let port: FakeTap['port'] | null = null;

beforeEach(() => {
  now = 0;
  port = null;
  vi.spyOn(performance, 'now').mockImplementation(() => now);
  vi.stubGlobal('document', { addEventListener: noop, removeEventListener: noop, visibilityState: 'visible' });
  const track = { label: 'fake', getSettings: () => ({}), stop: noop, onended: null };
  vi.stubGlobal('navigator', {
    mediaDevices: {
      getUserMedia: () => Promise.resolve({ getAudioTracks: () => [track], getTracks: () => [track] }),
      enumerateDevices: () => Promise.resolve([]),
    },
  });
  vi.stubGlobal(
    'AudioContext',
    class {
      state = 'running';
      sampleRate = RATE;
      audioWorklet = { addModule: () => Promise.resolve() };
      onstatechange: (() => void) | null = null;
      createMediaStreamSource() {
        return { connect: noop, disconnect: noop };
      }
      close() {
        return Promise.resolve();
      }
      resume() {
        return Promise.resolve();
      }
    },
  );
  vi.stubGlobal(
    'AudioWorkletNode',
    class {
      port: FakeTap['port'] = { onmessage: null, close: noop };
      constructor() {
        port = this.port;
      }
      disconnect = noop;
    },
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

type Heard = { kind: 'pause' | 'utterance'; at: number; bytes: number; offsetMs: number; silentMs?: number };

async function listening(opts: { line?: boolean } = {}) {
  const heard: Heard[] = [];
  const pieces: number[] = [];
  const sounds: string[] = [];
  let dropped = 0;
  const handlers: ListenerHandlers = {
    onLevel: noop,
    onSoundStart: () => sounds.push('start'),
    onSoundEnd: () => sounds.push('end'),
    onSpeechStart: noop,
    onPause: (audio, at, offsetMs) => heard.push({ kind: 'pause', at, bytes: audio.size, offsetMs: offsetMs ?? 0 }),
    onPiece: (at) => {
      pieces.push(at);
      return true;
    },
    onUtterance: (audio, at, offsetMs, silentMs) => heard.push({ kind: 'utterance', at: at ?? -1, bytes: audio.size, offsetMs: offsetMs ?? 0, ...(silentMs !== undefined ? { silentMs } : {}) }),
    onSpeechDropped: () => (dropped += 1),
    onError: noop,
  };
  const listener = new MicListener(opts);
  await listener.start(handlers);
  /** Push `ms` of sound at `amplitude` (a 200 Hz tone; 0 is silence). */
  const feed = (amplitude: number, ms: number) => {
    for (let done = 0; done < ms; done += FRAME_MS) {
      const n = (FRAME_MS / 1000) * RATE;
      const frame = new Float32Array(n);
      const first = Math.round((now / 1000) * RATE);
      for (let i = 0; i < n; i++) frame[i] = amplitude * Math.sin((2 * Math.PI * 200 * (first + i)) / RATE);
      now += FRAME_MS;
      port?.onmessage?.({ data: frame });
    }
  };
  feed(0, 1000); // the room, while the floor warms up
  return { listener, heard, pieces, feed, sounds, dropped: () => dropped };
}

describe('MicListener', () => {
  it('five minutes of talk with no pause at all: cut into pieces of 20 s, every request small, nothing lost', async () => {
    const t = await listening();
    t.feed(0.3, 300_000);
    t.feed(0, 800);
    // A piece every 20 s (the 30 s utterance ceiling it replaces ended the turn instead).
    // (A steady tone has no quieter moment, so each cut may come up to 1.5 s early.)
    expect(t.pieces.length).toBeGreaterThanOrEqual(14);
    for (let i = 1; i < t.pieces.length; i++) {
      const gap = (t.pieces[i] ?? 0) - (t.pieces[i - 1] ?? 0);
      expect(gap).toBeGreaterThanOrEqual(18_400);
      expect(gap).toBeLessThanOrEqual(20_100);
    }
    const pauses = t.heard.filter((h) => h.kind === 'pause');
    // Each piece went through a pause first (its recognition), then the real pause at the end.
    expect(pauses.map((p) => p.at).slice(0, t.pieces.length)).toEqual(t.pieces);
    expect(pauses).toHaveLength(t.pieces.length + 1);
    // The ring never ran over: every request had its audio, and none was bigger than 28 s.
    for (const h of t.heard) expect(h.bytes).toBeLessThanOrEqual(MAX_REQUEST_BYTES);
    const end = t.heard.filter((h) => h.kind === 'utterance');
    expect(end).toHaveLength(1);
    expect(end[0]?.bytes).toBeGreaterThan(WAV_HEADER);
    expect(t.dropped()).toBe(0);
    // The patience counts from the silence: the release had run 500 ms of it already.
    expect(end[0]?.silentMs).toBeGreaterThanOrEqual(480);
    expect(end[0]?.silentMs).toBeLessThanOrEqual(540);
  });

  it('a forced piece is cut at the quietest moment, not mid-syllable', async () => {
    const t = await listening();
    const start = now;
    t.feed(0.3, 19_000);
    const dip = now + 30; // the middle of a 60 ms dip, far too short to be a pause
    t.feed(0, 60);
    t.feed(0.3, 2_000);
    expect(t.pieces).toHaveLength(1);
    expect(Math.abs((t.pieces[0] ?? 0) - dip)).toBeLessThanOrEqual(30);
    expect(t.pieces[0] ?? 0).toBeGreaterThan(start);
  });

  it("the turn model's context reaches back across a pause into the same turn, but not into the last one sent", async () => {
    const t = await listening();
    t.feed(0.3, 2000); // "и освен това…"
    t.feed(0, 1500); // a thinking pause: the utterance closes, the turn is held
    t.feed(0.3, 1000);
    t.feed(0, 800);
    const [, second] = t.heard.filter((h) => h.kind === 'utterance');
    // Recognised from its own start, with the pause and the words before it as context.
    expect(second?.offsetMs).toBeGreaterThan(1500);
    t.listener.turnSent();
    t.feed(0, 1000);
    t.feed(0.3, 1000);
    t.feed(0, 800);
    const third = t.heard.filter((h) => h.kind === 'utterance')[2];
    expect(third?.offsetMs).toBeLessThanOrEqual(1000);
  });

  it("the context never holds Jarvis's voice: it starts where he stopped speaking", async () => {
    const t = await listening();
    t.feed(0.3, 2000);
    t.feed(0, 800);
    t.listener.setSpeaking(true);
    t.feed(0, 3000); // Jarvis answers (on the earpiece; what reaches the microphone is his)
    t.listener.setSpeaking(false);
    t.feed(0, 500);
    t.feed(0.3, 1000);
    t.feed(0, 800);
    const last = t.heard.filter((h) => h.kind === 'utterance').at(-1);
    // 500 ms of quiet after him, less the 300 ms pre-roll: nothing from before he stopped.
    expect(last?.offsetMs).toBeLessThanOrEqual(300);
  });

  it('the first sound of the next sentence is reported before it is speech, and its end if it was nothing', async () => {
    const t = await listening();
    t.feed(0.3, 2000);
    t.feed(0, 800); // the utterance closes
    const before = t.sounds.length; // the first sentence's own onset
    t.feed(0.3, 60); // he starts again: an onset, long before the attack holds
    expect(t.sounds.slice(before)).toEqual(['start']);
    t.feed(0, 600); // it was a click
    expect(t.sounds.slice(before)).toEqual(['start', 'end']);
  });

  it('a cut-in arrives whole: "Чакай." before the gap is not cut off by the attack starting over', async () => {
    // Alpha 2026-11-14 (F-02, ec5): over his voice the 450 ms cut-in attack restarted at the gap
    // after "Чакай.", so the utterance - and Whisper - began at "Спри за малко".
    const t = await listening({ line: true });
    t.listener.setSpeaking(true, true);
    const start = now;
    t.feed(0.3, 360); // "Чакай."
    t.feed(0, 200);
    t.feed(0.3, 1200); // "Спри за малко"
    t.listener.setSpeaking(false);
    t.feed(0, 800);
    const [cut] = t.heard.filter((h) => h.kind === 'utterance');
    // From the pre-roll before "Чакай" to the release that closed it (300 ms short of now).
    const fromWait = ((now - start) / 1000) * RATE * 2;
    expect(cut?.bytes).toBeGreaterThanOrEqual(WAV_HEADER + fromWait - 2 * RATE * 2 * 0.05);
  });

  it('a short sentence over the hold music reaches Whisper whole, though its attack never held', async () => {
    // Alpha round 2 (2026-11-15, R2-01): "Чакай, спри за малко." during thinking was dropped unheard.
    const t = await listening({ line: true });
    t.listener.setHold(true); // the hold music: the 450 ms attack
    const start = now;
    for (const [level, ms] of [[0.3, 360], [0, 120], [0.3, 360], [0, 120], [0.3, 360]] as const) t.feed(level, ms);
    const spoke = now - start;
    t.feed(0, 800);
    const heard = t.heard.filter((h) => h.kind === 'utterance');
    expect(heard).toHaveLength(1);
    expect(t.dropped()).toBe(0);
    // All of it, from the pre-roll before the first word, to the end of the silence that closed it.
    expect(heard[0]?.bytes).toBeGreaterThanOrEqual(WAV_HEADER + ((spoke + 300) / 1000) * RATE * 2);
  });

  it('muted mid-sentence: what he said up to the tap is an utterance, not thrown away', async () => {
    const t = await listening();
    t.feed(0.3, 2500);
    t.listener.setMuted(true);
    const end = t.heard.filter((h) => h.kind === 'utterance');
    expect(end).toHaveLength(1);
    expect(end[0]?.bytes).toBeGreaterThan(WAV_HEADER + 2 * RATE * 2);
    expect(t.dropped()).toBe(0);
  });
});
