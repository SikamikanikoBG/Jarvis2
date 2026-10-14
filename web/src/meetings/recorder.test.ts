import { describe, expect, it } from 'vitest';
import { quietestCut } from './recorder';

describe('quietestCut', () => {
  it('cuts at the quietest 100 ms between the minimum and the maximum, never before the minimum', () => {
    const levels = Array.from({ length: 450 }, (_, i) => (i === 120 ? 0.0001 : i === 390 ? 0.001 : 0.2));
    expect(quietestCut(levels, 300, 450)).toBe(390); // the quieter moment at 12 s is too early
  });
  it('with no quiet moment it cuts as soon as the piece is long enough', () => {
    expect(quietestCut(Array.from({ length: 450 }, () => 0.2), 300, 450)).toBe(300);
  });
});
