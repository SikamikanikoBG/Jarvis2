import { describe, expect, it } from 'vitest';
import { TTL_CHOICES, privacyOf, timeLeft, ttlLabel } from './privacy';

describe('privacy', () => {
  it('names the three offered idle times and falls back to plain units', () => {
    expect(TTL_CHOICES.map((c) => ttlLabel(c.seconds))).toEqual(['1 hour', '1 day', '1 week']);
    expect(ttlLabel(null)).toBe('never');
    expect(ttlLabel(120)).toBe('2 min');
    expect(ttlLabel(7_200)).toBe('2 h');
    expect(ttlLabel(172_800)).toBe('2 d');
  });

  it('says how long is left in the unit that reads best', () => {
    const now = Date.parse('2026-09-10T10:00:00Z');
    const at = (ms: number) => new Date(now + ms).toISOString();
    expect(timeLeft(at(30_000), now)).toBe('1 min'); // rounds up: never "0 min" while alive
    expect(timeLeft(at(58 * 60_000), now)).toBe('58 min');
    expect(timeLeft(at(3 * 3_600_000), now)).toBe('3 h');
    expect(timeLeft(at(47 * 3_600_000), now)).toBe('47 h');
    expect(timeLeft(at(6 * 86_400_000), now)).toBe('6 d');
    expect(timeLeft(at(-5_000), now)).toBe('any moment');
    expect(timeLeft('not a date', now)).toBe('');
  });

  it('classifies a conversation by its two fields, incognito winning', () => {
    expect(privacyOf({ incognito: false, ttl_seconds: null })).toBe('normal');
    expect(privacyOf({ incognito: false, ttl_seconds: 86_400 })).toBe('disappearing');
    expect(privacyOf({ incognito: true, ttl_seconds: 3_600 })).toBe('incognito');
  });
});
