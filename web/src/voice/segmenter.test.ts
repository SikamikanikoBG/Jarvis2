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
    expect(r.events.map((e) => e.type)).toEqual(['speech-start']);
    r = feed(seg, 0.002, 400, r.t); // a breath: not the end yet
    expect(r.events).toEqual([]);
    r = feed(seg, 0.2, 800, r.t); // …more of the sentence
    r = feed(seg, 0.002, 800, r.t); // done
    const end = r.events.find((e) => e.type === 'speech-end');
    expect(end?.type === 'speech-end' ? end.durationMs : 0).toBeGreaterThan(2000);
  });

  it('drops a click and a cough', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.3, 120, r.t); // a click: shorter than the attack
    const afterClick = feed(seg, 0.002, 1000, r.t);
    expect([...r.events, ...afterClick.events]).toEqual([]);
    r = feed(seg, 0.3, 300, afterClick.t); // a cough: past the attack, under the minimum utterance
    const afterCough = feed(seg, 0.002, 1000, r.t);
    expect([...r.events, ...afterCough.events].map((e) => e.type)).toEqual(['speech-start', 'dropped']);
  });

  it('adapts its floor to a noisy room instead of calling the noise speech', () => {
    const seg = new Segmenter();
    let r = feed(seg, 0.05, 3000, 0); // a café: steady 0.05, well over minLevel
    expect(r.events).toEqual([]);
    expect(seg.noiseFloor).toBeGreaterThan(0.03);
    r = feed(seg, 0.3, 1000, r.t); // a voice well over it
    expect(r.events.map((e) => e.type)).toEqual(['speech-start']);
  });

  it('closes a runaway utterance at the ceiling and keeps going', () => {
    const seg = new Segmenter({ maxUtteranceMs: 2000 });
    let r = feed(seg, 0.002, 1000, 0);
    r = feed(seg, 0.2, 4500, r.t);
    const types = r.events.map((e) => e.type);
    expect(types.filter((t) => t === 'speech-end').length).toBeGreaterThanOrEqual(2);
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
