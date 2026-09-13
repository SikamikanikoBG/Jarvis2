import { describe, expect, it } from 'vitest';
import { EchoGate } from './gate';

const FLOOR = 0.004;
const FRAME = 43;

/** A synthetic TTS sentence in the microphone: syllables at 0.06–0.12, gaps at 0.02. */
function echoFrame(i: number): number {
  const syllable = i % 6 < 4;
  return syllable ? 0.06 + 0.06 * Math.abs(Math.sin(i)) : 0.02;
}

describe('EchoGate', () => {
  it('passes everything through when he is not speaking', () => {
    const g = new EchoGate();
    expect(g.pass(0.3, 0, FLOOR)).toBe(0.3);
    expect(g.pass(0.001, FRAME, FLOOR)).toBe(0.001);
  });

  it('never lets his own echo through, syllables and all — the laptop bug', () => {
    const g = new EchoGate();
    g.setSpeaking(true, 0, FLOOR);
    let leaked = 0;
    for (let i = 0; i < 200; i++) if (g.pass(echoFrame(i), i * FRAME, FLOOR) > 0) leaked += 1;
    expect(leaked).toBe(0);
    expect(g.echoPeak).toBeGreaterThan(0.1);
  });

  it('lets a voice clearly over the echo through, after the grace', () => {
    const g = new EchoGate();
    g.setSpeaking(true, 0, FLOOR);
    for (let i = 0; i < 40; i++) g.pass(echoFrame(i), i * FRAME, FLOOR); // the echo is learned
    // Arsen cuts in at three times the loudest echo, for half a second.
    const peak = g.echoPeak;
    let passed = 0;
    for (let i = 40; i < 52; i++) if (g.pass(peak * 3, i * FRAME, FLOOR) > 0) passed += 1;
    expect(passed).toBe(12);
    // …and the bar did not chase him up while he spoke.
    expect(g.echoPeak).toBeLessThan(peak * 1.05);
  });

  it('counts nothing in the first moments of a sentence, before the echo is known', () => {
    const g = new EchoGate();
    g.setSpeaking(true, 0, FLOOR);
    // A loud onset in the first 300 ms would look like a voice over a peak of zero.
    expect(g.pass(0.5, 100, FLOOR)).toBe(0);
    expect(g.pass(0.5, 300, FLOOR)).toBe(0);
  });

  it('a quiet sentence does not lower the bar to the noise floor', () => {
    const g = new EchoGate();
    g.setSpeaking(true, 0, FLOOR);
    for (let i = 0; i < 40; i++) g.pass(0.005, i * FRAME, FLOOR); // he is almost inaudible
    expect(g.pass(0.015, 41 * FRAME, FLOOR)).toBe(0); // a breath is not a cut-in
    expect(g.pass(0.08, 42 * FRAME, FLOOR)).toBeGreaterThan(0);
  });

  it('forgets the echo quickly once he stops, so the next turn starts from the room', () => {
    const g = new EchoGate();
    g.setSpeaking(true, 0, FLOOR);
    for (let i = 0; i < 40; i++) g.pass(echoFrame(i), i * FRAME, FLOOR);
    g.setSpeaking(false, 41 * FRAME, FLOOR);
    for (let i = 41; i < 80; i++) g.pass(0.003, i * FRAME, FLOOR);
    expect(g.echoPeak).toBeLessThan(0.01);
  });
});
