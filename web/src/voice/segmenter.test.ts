import { describe, expect, it } from 'vitest';
import { Segmenter, rmsOf, type SegmentEvent } from './segmenter';

/** Feed a level for `ms` milliseconds in 40 ms frames; collect the events. */
function feed(seg: Segmenter, level: number, ms: number, from: number): { events: SegmentEvent[]; t: number } {
  const events: SegmentEvent[] = [];
  let t = from;
  for (; t < from + ms; t += 40) events.push(...seg.push(level, t));
  return { events, t };
}

describe('Segmenter', () => {
  it('opens on sustained speech, closes after the release silence, and reports the utterance', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0); // a quiet room: the floor settles
    expect(r.events).toEqual([]);
    r = feed(seg, 0.2, 1200, r.t); // a sentence
    expect(r.events.map((e) => e.type)).toEqual(['onset', 'speech-start']);
    r = feed(seg, 0.002, 400, r.t); // a breath: not the end yet, but maybe — a pause is reported
    expect(r.events.map((e) => e.type)).toEqual(['pause']);
    r = feed(seg, 0.2, 800, r.t); // …more of the sentence
    r = feed(seg, 0.002, 800, r.t); // done
    const pause = r.events.find((e) => e.type === 'pause');
    const end = r.events.find((e) => e.type === 'speech-end');
    expect(end?.type === 'speech-end' ? end.durationMs : 0).toBeGreaterThan(2000);
    // The end confirms the pause: same `at`, so a recognition started at the pause is the answer.
    expect(pause?.at).toBe(end?.at);
  });

  it('a pause is reported once per silence, and not for a fragment too short to be words', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.2, 300, r.t); // 300 ms of sound: under minUtteranceMs
    r = feed(seg, 0.002, 600, r.t);
    expect(r.events.filter((e) => e.type === 'pause')).toEqual([]);
    r = feed(seg, 0.2, 1000, r.t);
    r = feed(seg, 0.002, 700, r.t); // one pause, however long the release runs
    expect(r.events.filter((e) => e.type === 'pause')).toHaveLength(1);
  });

  it('drops a click and a cough', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.3, 120, r.t); // a click: shorter than the attack
    const afterClick = feed(seg, 0.002, 1000, r.t);
    expect([...r.events, ...afterClick.events].map((e) => e.type)).toEqual(['onset', 'quiet']); // a sound, and then it was nothing
    r = feed(seg, 0.3, 300, afterClick.t); // a cough: past the attack, under the minimum utterance
    const afterCough = feed(seg, 0.002, 1000, r.t);
    expect([...r.events, ...afterCough.events].map((e) => e.type)).toEqual(['onset', 'speech-start', 'dropped']);
  });

  it('adapts its floor to a noisy room instead of calling the noise speech', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.05, 3000, 0); // a café: steady 0.05, well over minLevel
    expect(r.events).toEqual([]);
    expect(seg.noiseFloor).toBeGreaterThan(0.03);
    r = feed(seg, 0.3, 1000, r.t); // a voice well over it
    expect(r.events.map((e) => e.type)).toEqual(['onset', 'speech-start']);
  });

  it('a long utterance stays one utterance: three minutes of talk with breaths is not cut', () => {
    // 2026-11-14: the 30 s ceiling closed a briefing mid-sentence, every half minute a turn of its own.
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    const events: SegmentEvent[] = [];
    while (r.t < 181_000) {
      r = feed(seg, 0.2, 3000, r.t);
      events.push(...r.events);
      r = feed(seg, 0.002, 120, r.t); // a breath, shorter than a pause
      events.push(...r.events);
    }
    expect(events.map((e) => e.type)).toEqual(['onset', 'speech-start']);
    expect(seg.talking).toBe(false); // in a breath: a silence, not sound
    r = feed(seg, 0.2, 200, r.t);
    expect(seg.talking).toBe(true);
    r = feed(seg, 0.002, 800, r.t);
    const end = r.events.find((e) => e.type === 'speech-end');
    expect(end?.type === 'speech-end' ? end.durationMs : 0).toBeGreaterThan(170_000);
  });

  it('endAt closes at a pause that is still going on, and not at one he has talked past', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.2, 1200, r.t);
    r = feed(seg, 0.002, 320, r.t); // a pause, the release still running
    const pause = r.events.find((e) => e.type === 'pause');
    expect(pause).toBeDefined();
    const at = pause?.at ?? -1;
    const closed = seg.endAt(at);
    expect(closed.map((e) => e.type)).toEqual(['speech-end']);
    expect(closed[0]?.at).toBe(at);
    // Closed: the rest of the silence reports nothing more.
    expect(feed(seg, 0.002, 800, r.t).events).toEqual([]);

    // A pause he went on after cannot be closed any more.
    r = feed(seg, 0.2, 1200, r.t + 800);
    r = feed(seg, 0.002, 320, r.t);
    const breath = r.events.find((e) => e.type === 'pause')?.at ?? -1;
    r = feed(seg, 0.2, 400, r.t);
    expect(seg.endAt(breath)).toEqual([]);
  });

  it('a sound not yet speech is reported at once, and when it came to nothing', () => {
    // 2026-11-14 alpha: the session sent a turn while he had already started the next sentence,
    // because only a confirmed attack said so. The onset is reported on the first loud frame.
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.3, 80, r.t);
    expect(r.events).toEqual([{ type: 'onset', at: 1000 }]);
    r = feed(seg, 0.002, 400, r.t); // shorter than a release: it may still be words
    expect(r.events).toEqual([]);
    r = feed(seg, 0.002, 200, r.t);
    expect(r.events.map((e) => e.type)).toEqual(['quiet']);
  });

  it('an utterance starts at the first sound of it, not where a dip inside a word restarted the attack', () => {
    const seg = new Segmenter();
    seg.setAttack(450); // a cut-in over his voice
    let r = feed(seg, 0.002, 1000, 0);
    const events: SegmentEvent[] = [];
    // "Чакай." (360 ms), a 200 ms gap, then "Спри за малко" - the attack starts over at the gap.
    for (const [level, ms] of [[0.3, 360], [0.002, 200], [0.3, 1200]] as const) {
      r = feed(seg, level, ms, r.t);
      events.push(...r.events);
    }
    expect(events.map((e) => e.type)).toEqual(['onset', 'speech-start']);
    expect(events[1]?.at).toBe(1000); // "Чакай", not "Спри"
  });

  it('a long-ago click does not drag the start back: the reach is bounded', () => {
    const seg = new Segmenter({ onsetReachMs: 300 });
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.3, 40, r.t); // a click at 1000
    r = feed(seg, 0.002, 440, r.t); // not long enough to be quiet
    const start = r.t;
    r = feed(seg, 0.3, 400, r.t); // speech at 1480, more than 300 ms after the click
    expect(r.events.find((e) => e.type === 'speech-start')?.at).toBe(start);
  });

  it('a clattering room that never becomes speech is let go of, and does not start again until it is quiet', () => {
    // Bursts too short to be speech, too close together to be quiet: the turn must not wait on them.
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    const events: SegmentEvent[] = [];
    for (let i = 0; i < 12; i += 1) {
      r = feed(seg, 0.3, 120, r.t);
      events.push(...r.events);
      r = feed(seg, 0.002, 200, r.t);
      events.push(...r.events);
    }
    expect(events.map((e) => e.type)).toEqual(['onset', 'quiet']);
    r = feed(seg, 0.002, 600, r.t); // the room is quiet for a moment
    r = feed(seg, 0.3, 40, r.t);
    expect(r.events.map((e) => e.type)).toEqual(['onset']);
  });

  it('a cut-in that takes seconds to confirm under the long attack still starts at its first word', () => {
    // Alpha 2026-11-15: over the hold music the 450 ms attack took 2.7 s to hold on "Чакай, спри за
    // малко. Кажи ми…"; letting go of the sound at 1.5 s also lost where it began.
    const seg = new Segmenter();
    seg.setAttack(450);
    let r = feed(seg, 0.002, 1000, 0);
    const events: SegmentEvent[] = [];
    for (let i = 0; i < 5; i += 1) {
      r = feed(seg, 0.3, 360, r.t); // syllables, never 450 ms without a dip
      events.push(...r.events);
      r = feed(seg, 0.002, 120, r.t);
      events.push(...r.events);
    }
    r = feed(seg, 0.3, 600, r.t);
    events.push(...r.events);
    // A held turn is let go of after 1.5 s; the utterance still begins at the first word.
    expect(events.map((e) => e.type)).toEqual(['onset', 'quiet', 'speech-start']);
    expect(events.find((e) => e.type === 'speech-start')?.at).toBe(1000);
  });

  it('end() closes what is open', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.2, 1000, r.t);
    expect(seg.end(r.t).map((e) => e.type)).toEqual(['speech-end']);
    expect(seg.speaking).toBe(false);
  });
});

describe('rmsOf', () => {
  it('is 0 for silence (128s) and grows with amplitude', () => {
    expect(rmsOf(new Uint8Array(64).fill(128))).toBe(0);
    const loud = new Uint8Array(64).map((_, i) => (i % 2 ? 255 : 1));
    expect(rmsOf(loud)).toBeGreaterThan(0.9);
  });
});

describe('wavBlob', () => {
  it('writes a valid 16 kHz mono PCM header around the samples', async () => {
    const { wavBlob } = await import('./listener');
    const blob = wavBlob(new Int16Array([0, 1000, -1000, 0]), 16_000);
    expect(blob.type).toBe('audio/wav');
    expect(blob.size).toBe(44 + 8);
    const v = new DataView(await blob.arrayBuffer());
    const tag = (at: number) => String.fromCharCode(v.getUint8(at), v.getUint8(at + 1), v.getUint8(at + 2), v.getUint8(at + 3));
    expect([tag(0), tag(8), tag(12), tag(36)]).toEqual(['RIFF', 'WAVE', 'fmt ', 'data']);
    expect(v.getUint32(24, true)).toBe(16_000);
    expect(v.getUint16(22, true)).toBe(1);
    expect(v.getUint32(40, true)).toBe(8);
    expect(v.getInt16(46, true)).toBe(1000);
  });
});
