import { describe, expect, it } from 'vitest';
import { PATIENCE_CHOICES, patienceChoices, patienceHint, patienceMs } from './patience';

describe('patienceMs', () => {
  it('is the setting in milliseconds', () => {
    expect(patienceMs({ turn_patience_s: 5 })).toBe(5000);
    expect(patienceMs({ turn_patience_s: 1.5 })).toBe(1500);
  });
  it('is 3 s when the core does not say (an older core, settings that could not be read)', () => {
    expect(patienceMs({})).toBe(3000);
    expect(patienceMs(null)).toBe(3000);
    expect(patienceMs({ turn_patience_s: Number.NaN })).toBe(3000);
  });
  it('is held to 1-8 s, as the core validates it', () => {
    expect(patienceMs({ turn_patience_s: 0.2 })).toBe(1000);
    expect(patienceMs({ turn_patience_s: 99 })).toBe(8000);
  });
});

describe('the patience in Settings → Voice', () => {
  it('offers 1-8 s with 3 s among them, and a value set some other way beside them', () => {
    expect(PATIENCE_CHOICES).toContain(3);
    expect(Math.min(...PATIENCE_CHOICES)).toBe(1);
    expect(Math.max(...PATIENCE_CHOICES)).toBe(8);
    expect(patienceChoices({ turn_patience_s: 3 })).toEqual(PATIENCE_CHOICES);
    expect(patienceChoices({ turn_patience_s: 2.5 })).toContain(2.5);
  });
  it('says in seconds what it means, with the turn model and without it', () => {
    expect(patienceHint({ turn_patience_s: 3, turn_detection: true })).toMatch(/unfinished.*up to 3 s/);
    expect(patienceHint({ turn_patience_s: 5, turn_detection: false })).toMatch(/every pause waits 5 s/);
    expect(patienceHint({ turn_detection: true })).toMatch(/3 s/); // an older core: the default
  });
});
