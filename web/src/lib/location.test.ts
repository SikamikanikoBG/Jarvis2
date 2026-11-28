import { describe, expect, it } from 'vitest';
import type { DeviceLocation } from '../protocol/types';
import { distanceM, shouldPost } from './location';

const fix = (lat: number, lon: number): DeviceLocation => ({ lat, lon, accuracy_m: 10, taken_at: new Date().toISOString(), source: 'browser', speed_mps: null });

describe('location', () => {
  it('measures metres between fixes', () => {
    expect(distanceM(fix(42.68, 23.32), fix(42.68, 23.32))).toBe(0);
    expect(Math.round(distanceM(fix(42.68, 23.32), fix(42.681, 23.32)))).toBe(111);
  });

  it('posts the first fix, then on ~100 m moved or every ten minutes', () => {
    const now = 1_000_000_000;
    expect(shouldPost(fix(42.68, 23.32), null, 0, now)).toBe(true);
    expect(shouldPost(fix(42.6803, 23.32), fix(42.68, 23.32), now - 60_000, now)).toBe(false); // 33 m, a minute
    expect(shouldPost(fix(42.681, 23.32), fix(42.68, 23.32), now - 60_000, now)).toBe(true); // 111 m
    expect(shouldPost(fix(42.68, 23.32), fix(42.68, 23.32), now - 11 * 60_000, now)).toBe(true); // ten minutes
  });

  it('posts every few metres and seconds while guiding', () => {
    const now = 1_000_000_000;
    expect(shouldPost(fix(42.68007, 23.32), fix(42.68, 23.32), now - 1_000, now, true)).toBe(false); // 8 m? no: 7.8 m, 1 s
    expect(shouldPost(fix(42.6801, 23.32), fix(42.68, 23.32), now - 1_000, now, true)).toBe(true); // 11 m
    expect(shouldPost(fix(42.68, 23.32), fix(42.68, 23.32), now - 5_000, now, true)).toBe(true); // 5 s
  });
});
