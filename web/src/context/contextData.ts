/** Pure helpers for the Context screen: names and colours per source, number and time formats,
 *  the chart's metrics. Kept free of React so they are tested on their own. */
import type { ContextUsage, OverflowKind, UsageSeries } from '../protocol/types';

const SOURCE_NAMES: Record<string, string> = {
  chat: 'Chat',
  voice: 'Voice calls',
  scheduled: 'Scheduled',
  task: 'Tasks',
  triage: 'Triage',
  mail: 'Mail desk',
  meeting: 'Meetings',
  collab: 'Collab',
  system: 'System',
  housekeeping: 'Housekeeping',
};

/** "scheduled:Morning brief" → "Morning brief"; "housekeeping:journal" → "Housekeeping · journal". */
export function sourceName(key: string): string {
  const at = key.indexOf(':');
  if (at < 0) return SOURCE_NAMES[key] ?? key;
  const source = key.slice(0, at);
  const label = key.slice(at + 1);
  return source === 'scheduled' ? label : `${SOURCE_NAMES[source] ?? source} · ${label}`;
}

/** What kind of thing a source is, for the second line of a legend entry. */
export function sourceKind(key: string): string {
  const source = key.split(':')[0];
  return source === 'scheduled' ? 'Scheduled prompt' : (SOURCE_NAMES[source] ?? source);
}

// Mid-saturation hues that read on both the dark and the light theme.
const FIXED: Record<string, string> = {
  chat: '#25b8e6',
  voice: '#e05fa8',
  task: '#8b7cf6',
  triage: '#e0a030',
  mail: '#3fae6a',
  meeting: '#d9734a',
  collab: '#5a8ff0',
  system: '#9aa3b5',
  housekeeping: '#7f8aa3',
};
const PALETTE = ['#c9a227', '#2bb39a', '#b96ad9', '#e76f51', '#4f9de0', '#9bc53d', '#d1495b', '#00a6a6', '#f28f3b', '#6c8ead'];

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

/** A source keeps its colour across reloads and ranges (keyed by name, not by position). */
export function colorFor(key: string): string {
  if (FIXED[key]) return FIXED[key];
  if (key.startsWith('housekeeping:')) return FIXED.housekeeping;
  return PALETTE[hash(key) % PALETTE.length];
}

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (n < 1000) return String(Math.round(n));
  if (n < 10_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
}

/** Share of the window, 0-100+ (over 100 is an overflow and is shown as such). Null when the
 *  window is unknown. */
export function pctOf(used: number, window: number | null | undefined): number | null {
  if (!window) return null;
  return (used / window) * 100;
}

export function fmtPct(p: number | null): string {
  if (p === null) return '—';
  if (p > 0 && p < 1) return '<1%';
  return `${Math.round(p)}%`;
}

export function fmtMinutes(ms: number): string {
  const m = ms / 60000;
  if (m < 1) return `${Math.round(ms / 1000)}s`;
  if (m < 60) return `${Math.round(m)} min`;
  return `${(m / 60).toFixed(1).replace(/\.0$/, '')} h`;
}

export function fmtBucket(ts: number, bucketS: number, tz: string): string {
  const d = new Date(ts);
  const opts: Intl.DateTimeFormatOptions =
    bucketS >= 86400
      ? { weekday: 'short', day: 'numeric', month: 'short', timeZone: tz }
      : bucketS >= 3 * 3600
        ? { weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: tz }
        : { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: tz };
  return new Intl.DateTimeFormat('en-GB', opts).format(d);
}

export function fmtTime(ts: number, tz: string): string {
  return new Intl.DateTimeFormat('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: tz }).format(new Date(ts));
}

export type Metric = 'peak' | 'busy' | 'tokens';

export const METRICS: { id: Metric; label: string; hint: string }[] = [
  { id: 'peak', label: 'Peak context', hint: 'Largest single call in each period, against the window (dashed). Near the line = you need more context.' },
  { id: 'busy', label: 'Busy', hint: 'Share of each period the model was working for this source. High and stacked = move a schedule.' },
  { id: 'tokens', label: 'Tokens processed', hint: 'Prompt tokens actually read (not served from cache) plus tokens written.' },
];

/** The values a series contributes to the chart for one metric. Busy is a percentage of the
 *  period's real length (a DST day is 23 or 25 hours; the last period ends now). */
export function seriesValues(s: UsageSeries, metric: Metric, lengths: number[]): number[] {
  if (metric === 'peak') return s.peak;
  if (metric === 'tokens') return s.tokens;
  return s.busy_ms.map((b, i) => Math.min(100, (b / Math.max(1, lengths[i] ?? 1)) * 100));
}

/** A round axis maximum at or above ``v`` (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 × a power of ten),
 *  tight enough that a 98k window does not leave half the chart empty. */
export function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

/** The largest window any endpoint had in the range, for the peak chart's reference line. */
export function windowLines(u: ContextUsage): { endpoint: string; values: (number | null)[] }[] {
  return Object.entries(u.windows).map(([endpoint, values]) => ({ endpoint, values }));
}

/** Busy minutes per hour of day, all sources together, and the quietest hours (for moving a
 *  schedule): the hours whose total is lowest, in order. */
export function hourTotals(profile: Record<string, number[]>): number[] {
  const out = new Array<number>(24).fill(0);
  for (const bins of Object.values(profile)) bins.forEach((v, h) => (out[h] += v));
  return out;
}

export function quietestHours(totals: number[], n = 3): number[] {
  return totals
    .map((v, h) => ({ v, h }))
    .sort((a, b) => a.v - b.v || a.h - b.h)
    .slice(0, n)
    .map((x) => x.h)
    .sort((a, b) => a - b);
}

export const OVERFLOW_LABELS: Record<OverflowKind, { label: string; hint: string }> = {
  rejected: { label: 'Refused', hint: 'The server refused the prompt: it did not fit the context window.' },
  shrunk: { label: 'Answer room cut', hint: 'The prompt fitted only with less room for the answer; Jarvis retried with less.' },
  trimmed: { label: 'History trimmed', hint: 'Older conversation was dropped before the call to fit the budget.' },
  cut: { label: 'Answer cut off', hint: 'The answer stopped at the output limit.' },
};

export const OVERFLOW_ORDER: OverflowKind[] = ['rejected', 'shrunk', 'trimmed', 'cut'];

/** Short host:port of an endpoint URL, for labels. */
export function endpointName(url: string): string {
  try {
    const u = new URL(url);
    return u.port ? `${u.hostname}:${u.port}` : u.hostname;
  } catch {
    return url;
  }
}
