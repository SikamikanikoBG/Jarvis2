import { describe, expect, it } from 'vitest';
import { colorFor, fmtPct, fmtTokens, hourTotals, niceMax, pctOf, quietestHours, seriesValues, sourceName, fmtBucket, endpointName } from './contextData';

describe('context data helpers', () => {
  it('names sources, scheduled prompts by their own name', () => {
    expect(sourceName('chat')).toBe('Chat');
    expect(sourceName('scheduled:Morning brief')).toBe('Morning brief');
    expect(sourceName('housekeeping:journal')).toBe('Housekeeping · journal');
    expect(sourceName('scheduled:a:b')).toBe('a:b');
    expect(sourceName('weird')).toBe('weird');
  });

  it('keeps a colour per source, whatever order they come in', () => {
    expect(colorFor('scheduled:News')).toBe(colorFor('scheduled:News'));
    expect(colorFor('chat')).not.toBe(colorFor('voice'));
    expect(colorFor('housekeeping:journal')).toBe(colorFor('housekeeping'));
  });

  it('formats tokens and percentages, unknown window as a dash', () => {
    expect(fmtTokens(950)).toBe('950');
    expect(fmtTokens(1200)).toBe('1.2k');
    expect(fmtTokens(98304)).toBe('98k');
    expect(fmtTokens(1_500_000)).toBe('1.5M');
    expect(fmtTokens(null)).toBe('—');
    expect(pctOf(49152, 98304)).toBe(50);
    expect(pctOf(10, null)).toBeNull();
    expect(fmtPct(null)).toBe('—');
    expect(fmtPct(0.3)).toBe('<1%');
    expect(fmtPct(112.4)).toBe('112%');
  });

  it('busy is a share of each period, capped at 100', () => {
    const s = { source: 'chat', label: null, peak: [1, 2], calls: [1, 1], tokens: [5, 6], busy_ms: [1800_000, 7200_000], overflows: [0, 0] };
    expect(seriesValues(s, 'busy', [3600_000, 3600_000])).toEqual([50, 100]);
    expect(seriesValues(s, 'peak', [])).toEqual([1, 2]);
    expect(seriesValues(s, 'tokens', [])).toEqual([5, 6]);
  });

  it('rounds the axis up to a nice number', () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(98304)).toBe(100000);
    expect(niceMax(130000)).toBe(150000);
    expect(niceMax(103000)).toBe(120000);
    expect(niceMax(23)).toBe(25);
  });

  it('finds the quietest hours across every source', () => {
    const a = new Array<number>(24).fill(5);
    const b = new Array<number>(24).fill(0);
    a[3] = 0;
    a[4] = 0;
    b[4] = 1;
    a[14] = 0;
    const totals = hourTotals({ a, b });
    expect(totals[4]).toBe(1);
    expect(quietestHours(totals, 3)).toEqual([3, 4, 14]);
    expect(quietestHours(totals, 2)).toEqual([3, 14]);
  });

  it('labels buckets in the asked timezone', () => {
    const ts = Date.UTC(2026, 10, 6, 10, 0);
    expect(fmtBucket(ts, 3600, 'Europe/Sofia')).toBe('12:00');
    expect(fmtBucket(ts, 86400, 'Europe/Sofia')).toMatch(/Fri/);
    expect(endpointName('http://100.76.27.18:18100/v1')).toBe('100.76.27.18:18100');
  });
});
