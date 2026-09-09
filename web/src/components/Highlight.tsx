import type { ReactNode } from 'react';

/**
 * The query's words wrapped in <mark> wherever they appear in `text`, so the eye lands on the
 * reason a row survived the filter instead of re-reading it.
 *
 * Matching is per word and case-insensitive, the same rule `matchesQuery` filters by — anything
 * else would highlight a different thing from what the filter kept.
 */
export function Highlight({ text, query }: { text: string; query: string }) {
  const terms = [...new Set(query.toLowerCase().split(/\s+/).filter(Boolean))];
  if (terms.length === 0) return <>{text}</>;

  // Every occurrence of every term, then merged so overlaps do not produce nested marks.
  const lower = text.toLowerCase();
  const spans: [number, number][] = [];
  for (const term of terms) {
    for (let at = lower.indexOf(term); at >= 0; at = lower.indexOf(term, at + term.length)) {
      spans.push([at, at + term.length]);
    }
  }
  if (spans.length === 0) return <>{text}</>;
  spans.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [start, end] of spans) {
    const last = merged.at(-1);
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }

  const out: ReactNode[] = [];
  let at = 0;
  for (const [start, end] of merged) {
    if (start > at) out.push(text.slice(at, start));
    out.push(
      <mark key={start} className="search-mark">
        {text.slice(start, end)}
      </mark>,
    );
    at = end;
  }
  if (at < text.length) out.push(text.slice(at));
  return <>{out}</>;
}
