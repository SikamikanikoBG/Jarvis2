import { describe, expect, it } from 'vitest';
import { goOnDrain, phaseLabel } from './callView';

const base = { phase: 'listening' as const, muted: false, online: true, goOn: null };

describe('phaseLabel', () => {
  it('says "Listening — go on" while the turn waits for him, and only then', () => {
    expect(phaseLabel({ ...base, goOn: { until: 3000, ms: 3000 } })).toBe('Listening — go on');
    expect(phaseLabel(base)).toBe('Listening');
    expect(phaseLabel({ ...base, phase: 'thinking', goOn: null })).toBe('Thinking…');
  });
  it('mute and a lost connection still come first', () => {
    expect(phaseLabel({ ...base, muted: true, goOn: { until: 1, ms: 1 } })).toBe('Muted');
    expect(phaseLabel({ ...base, online: false, goOn: { until: 1, ms: 1 } })).toBe('No connection — reconnecting…');
  });
});

describe('goOnDrain', () => {
  it('runs over the whole wait, starting where the wait already is', () => {
    expect(goOnDrain({ until: 10_000, ms: 3000 }, 7000)).toEqual({ durationMs: 3000, delayMs: 0 });
    expect(goOnDrain({ until: 10_000, ms: 3000 }, 8000)).toEqual({ durationMs: 3000, delayMs: -1000 });
  });
  it('a wait already over, or a clock that is off, is held to the wait', () => {
    expect(goOnDrain({ until: 10_000, ms: 3000 }, 12_000).delayMs).toBe(-3000);
    expect(goOnDrain({ until: 10_000, ms: 3000 }, 0).delayMs).toBe(0);
  });
});
