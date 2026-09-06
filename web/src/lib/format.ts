/** Formatting helpers. Pure, no locale surprises: tabular numbers for the instrument panel. */

export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function formatDuration(ms: number): string {
  if (ms < 0) return '0ms';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  if (m < 60) return `${m}m ${s.toString().padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2, '0')}m`;
}

export function formatSeconds(ms: number): string {
  return `${Math.max(0, Math.floor(ms / 1000))}s`;
}

export function formatClock(ts: string | number, withMs = false): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const hh = d.getHours().toString().padStart(2, '0');
  const mm = d.getMinutes().toString().padStart(2, '0');
  const ss = d.getSeconds().toString().padStart(2, '0');
  return withMs ? `${hh}:${mm}:${ss}.${d.getMilliseconds().toString().padStart(3, '0')}` : `${hh}:${mm}:${ss}`;
}

export function formatRelative(ts: string, now = Date.now()): string {
  const t = Date.parse(ts);
  if (Number.isNaN(t)) return '';
  const diff = Math.max(0, now - t);
  const min = Math.floor(diff / 60_000);
  if (min < 1) return 'now';
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d`;
  const date = new Date(t);
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  return date.toLocaleDateString(undefined, sameYear ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: '2-digit' });
}

/** Markdown → plain text for one-line previews (headings, emphasis, code ticks, links). */
export function stripMarkdown(src: string): string {
  return src
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, '')
    .replace(/(\*\*|__|\*|_|~~)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** One-line preview of a JSON-ish value, capped. */
export function previewValue(value: unknown, max = 90): string {
  let s: string;
  if (typeof value === 'string') s = value;
  else {
    try {
      s = JSON.stringify(value) ?? '';
    } catch {
      s = String(value);
    }
  }
  s = s.replace(/\s+/g, ' ').trim();
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** `{a: 1, b: "x"}` → `a=1, b="x"` for tool-card headers. */
export function summariseArgs(args: Record<string, unknown>, max = 80): string {
  const parts = Object.entries(args).map(([k, v]) => `${k}=${previewValue(v, 40)}`);
  const s = parts.join(', ');
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value);
  }
}

export function tokPerSec(completionTokens: number, durationMs: number, ttftMs: number | null): number | null {
  const gen = durationMs - (ttftMs ?? 0);
  if (gen <= 0 || completionTokens <= 0) return null;
  return (completionTokens / gen) * 1000;
}

/** Human file size for an attachment chip. */
export function sizeLabel(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} kB`;
  return `${bytes} B`;
}
