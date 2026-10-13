import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HoldMusic } from './hold';
import { ToneCues } from './tones';

/** Just enough of an AudioContext to see what would sound, and when. */
class FakeParam {
  value = 1;
  ramps: { to: number; at: number }[] = [];
  setValueAtTime(v: number): void {
    this.value = v;
  }
  exponentialRampToValueAtTime(to: number, at: number): void {
    this.ramps.push({ to, at });
  }
  cancelScheduledValues(): void {
    this.ramps = [];
  }
}

class FakeNode {
  gain = new FakeParam();
  frequency = new FakeParam();
  type = '';
  started: number[] = [];
  disconnected = false;
  connect<T>(next: T): T {
    return next;
  }
  disconnect(): void {
    this.disconnected = true;
  }
  start(at: number): void {
    this.started.push(at);
  }
  stopped = 0;
  stop(): void {
    this.stopped += 1;
  }
}

class FakeContext {
  currentTime = 0;
  state: AudioContextState = 'running';
  destination = new FakeNode();
  oscillators: FakeNode[] = [];
  gains: FakeNode[] = [];
  createGain(): FakeNode {
    const g = new FakeNode();
    this.gains.push(g);
    return g;
  }
  createOscillator(): FakeNode {
    const o = new FakeNode();
    this.oscillators.push(o);
    return o;
  }
  createBiquadFilter(): FakeNode {
    return new FakeNode();
  }
}

const asCtx = (c: FakeContext) => c as unknown as AudioContext;

describe('HoldMusic', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('fades in, keeps scheduling bars ahead of the clock, and fades out when stopped', () => {
    const ctx = new FakeContext();
    const music = new HoldMusic(asCtx(ctx), { volume: 0.4, random: () => 0.9 });
    music.start();
    expect(music.playing).toBe(true);
    const master = ctx.gains[0];
    expect(master?.gain.ramps[0]?.to).toBeGreaterThan(0.05); // up to its level...
    expect(master?.gain.ramps[0]?.at).toBeGreaterThan(1); // ...slowly
    const first = ctx.oscillators.length;
    expect(first).toBeGreaterThan(0); // the first bar is on the clock at once
    // Time passes: more bars are put on the clock, always ahead of it.
    for (let i = 0; i < 40; i += 1) {
      ctx.currentTime += 0.15;
      vi.advanceTimersByTime(150);
    }
    expect(ctx.oscillators.length).toBeGreaterThan(first * 2);
    expect(Math.max(...ctx.oscillators.flatMap((o) => o.started))).toBeGreaterThan(ctx.currentTime);
    music.stop();
    expect(music.playing).toBe(false);
    expect(master?.gain.ramps.at(-1)?.to).toBeLessThan(0.001); // down to nothing...
    expect((master?.gain.ramps.at(-1)?.at ?? 99) - ctx.currentTime).toBeLessThan(0.5); // ...quickly
    const scheduled = ctx.oscillators.length;
    ctx.currentTime += 3;
    vi.advanceTimersByTime(5000);
    expect(ctx.oscillators.length).toBe(scheduled); // nothing new once stopped
    expect(master?.disconnected).toBe(true);
  });

  it('volume 0 is silence, not an error', () => {
    const ctx = new FakeContext();
    const music = new HoldMusic(asCtx(ctx), { volume: 0 });
    music.start();
    expect(ctx.gains[0]?.gain.ramps[0]?.to).toBeLessThan(0.001);
    music.stop();
  });
});

describe('ToneCues while he thinks', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('plays hold music only when the answer takes a while, and tells the listener', () => {
    const ctx = new FakeContext();
    const hold: boolean[] = [];
    const cues = new ToneCues({ contextOf: () => asCtx(ctx), waiting: 'music', onHold: (on) => hold.push(on) });
    cues.thinking(true);
    cues.thinking(false); // a quick answer: nothing at all
    vi.advanceTimersByTime(3000);
    expect(hold).toEqual([]);
    expect(ctx.oscillators).toHaveLength(0);
    cues.thinking(true);
    vi.advanceTimersByTime(1500);
    expect(hold).toEqual([true]);
    expect(ctx.oscillators.length).toBeGreaterThan(0);
    cues.thinking(false); // he speaks: the music goes
    expect(hold).toEqual([true, false]);
    cues.close();
  });

  it('silence is silence, and beeps are still there for whoever prefers them', () => {
    const ctx = new FakeContext();
    const quiet = new ToneCues({ contextOf: () => asCtx(ctx), waiting: 'silence' });
    quiet.thinking(true);
    vi.advanceTimersByTime(6000);
    expect(ctx.oscillators).toHaveLength(0);
    quiet.close();
    const beeps = new ToneCues({ contextOf: () => asCtx(ctx), waiting: 'beeps', suppress: () => undefined });
    beeps.thinking(true);
    vi.advanceTimersByTime(1500);
    expect(ctx.oscillators).toHaveLength(2); // one soft pair
    beeps.close();
  });

  it('with no running audio the music is not attempted; nothing breaks', () => {
    const ctx = new FakeContext();
    ctx.state = 'suspended';
    const hold: boolean[] = [];
    const cues = new ToneCues({ contextOf: () => asCtx(ctx), waiting: 'music', onHold: (on) => hold.push(on) });
    cues.thinking(true);
    vi.advanceTimersByTime(4000);
    expect(hold).toEqual([]);
    expect(ctx.oscillators).toHaveLength(0);
    cues.close();
  });
});
