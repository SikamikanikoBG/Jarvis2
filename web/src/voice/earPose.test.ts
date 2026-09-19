import { describe, expect, it } from 'vitest';
import { classifyPose, EarTracker, ENTER_MS, LEAVE_MS, STALE_MS } from './earPose';

const G = 9.81;
/** Gravity in the phone's frame for a phone `tilt`° from upright (top leaning back) and the
 * glass `face`° from vertical toward the eyes. */
const pose = (tilt: number, face: number) => {
  const t = (tilt * Math.PI) / 180;
  const f = (face * Math.PI) / 180;
  return { x: G * Math.sin(t), y: G * Math.cos(t) * Math.cos(f), z: G * Math.cos(t) * Math.sin(f) };
};

describe('classifyPose', () => {
  it('a phone upright with its glass against a cheek is at the ear', () => {
    expect(classifyPose(pose(0, 0), false)).toBe(true);
    expect(classifyPose(pose(25, 5), false)).toBe(true);
  });
  it('a phone tilted back so the glass faces the eyes is not', () => {
    expect(classifyPose(pose(0, 35), false)).toBe(false);
    expect(classifyPose(pose(20, 25), false)).toBe(false);
  });
  it('a phone lying flat or nearly so is not at an ear', () => {
    expect(classifyPose({ x: 0, y: 0, z: G }, false)).toBe(false);
    expect(classifyPose(pose(60, 0), false)).toBe(false);
  });
  it('signs do not matter: upside down, or the other platform', () => {
    expect(classifyPose({ x: 0, y: -G, z: 0 }, false)).toBe(true);
    expect(classifyPose({ x: 0, y: -G * 0.8, z: -G * 0.6 }, false)).toBe(false);
  });
  it('hysteresis: a small tilt inside the ear pose does not leave it, a clear one does', () => {
    expect(classifyPose(pose(0, 15), true)).toBe(true); // 15° from vertical: still at the ear
    expect(classifyPose(pose(0, 15), false)).toBe(false); // …but not enough to enter it
    expect(classifyPose(pose(0, 25), true)).toBe(false);
  });
});

describe('EarTracker', () => {
  it('goes dark only after the pose has held, and back at once', () => {
    const t = new EarTracker();
    expect(t.feed(pose(0, 0), 0)).toBe(false);
    expect(t.feed(pose(0, 0), ENTER_MS - 50)).toBe(false);
    expect(t.feed(pose(0, 0), ENTER_MS + 10)).toBe(true);
    expect(t.feed(pose(0, 40), ENTER_MS + 20)).toBe(true);
    expect(t.feed(pose(0, 40), ENTER_MS + 20 + LEAVE_MS)).toBe(false);
  });
  it('a cheek brushing past does not count', () => {
    const t = new EarTracker();
    t.feed(pose(0, 0), 0);
    t.feed(pose(0, 40), 200); // back to looking: the candidate is dropped
    expect(t.feed(pose(0, 0), 300)).toBe(false);
    expect(t.feed(pose(0, 0), 300 + ENTER_MS - 1)).toBe(false);
    expect(t.feed(pose(0, 0), 300 + ENTER_MS)).toBe(true);
  });
  it('a sensor that stops talking cannot leave the screen dark', () => {
    const t = new EarTracker();
    t.feed(pose(0, 0), 0);
    expect(t.feed(pose(0, 0), ENTER_MS)).toBe(true);
    expect(t.tick(ENTER_MS + STALE_MS - 1)).toBe(true);
    expect(t.tick(ENTER_MS + STALE_MS + 1)).toBe(false);
  });
});
