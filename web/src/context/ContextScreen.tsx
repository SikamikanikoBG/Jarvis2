import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import { IconButton } from '../components/primitives';
import { errorText } from '../lib/useLoader';
import type { ContextLive, ContextRange, ContextUsage, LiveEndpoint, OverflowItem, OverflowKind } from '../protocol/types';
import { useStore } from '../store/store';
import {
  METRICS,
  type Metric,
  OVERFLOW_LABELS,
  OVERFLOW_ORDER,
  colorFor,
  endpointName,
  fmtBucket,
  fmtMinutes,
  fmtPct,
  fmtTime,
  fmtTokens,
  hourTotals,
  niceMax,
  pctOf,
  quietestHours,
  seriesValues,
  sourceKind,
  sourceName,
} from './contextData';

const LIVE_MS = 3000;
const USAGE_MS = 60_000;
const RANGES: { id: ContextRange; label: string }[] = [
  { id: '1h', label: 'Hour' },
  { id: '6h', label: '6 h' },
  { id: '24h', label: 'Day' },
  { id: '7d', label: 'Week' },
  { id: '30d', label: 'Month' },
  { id: '90d', label: '90 d' },
];
const HIDDEN_KEY = 'jarvis_context_hidden';

const browserTz = (): string => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
};

function readHidden(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) ?? '[]') as string[]);
  } catch {
    return new Set();
  }
}

function writeHidden(s: Set<string>) {
  try {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify([...s]));
  } catch {
    /* private mode: the toggle just is not remembered */
  }
}

/** Poll while the page is visible; `ms` apart, first call at once. */
function usePoll(fn: () => void, ms: number, deps: unknown[]) {
  useEffect(() => {
    fn();
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') fn();
    }, ms);
    const onVis = () => document.visibilityState === 'visible' && fn();
    document.addEventListener('visibilitychange', onVis);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVis);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export function ContextScreen() {
  const tz = useMemo(() => browserTz(), []);
  const [live, setLive] = useState<ContextLive | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [range, setRange] = useState<ContextRange>('24h');
  const [usage, setUsage] = useState<ContextUsage | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>('peak');
  const [hidden, setHidden] = useState<Set<string>>(readHidden);

  const loadLive = useCallback(() => {
    api.context
      .live()
      .then((l) => {
        setLive(l);
        setLiveError(null);
      })
      .catch((e: unknown) => setLiveError(errorText(e)));
  }, []);
  const loadUsage = useCallback(() => {
    api.context
      .usage(range, tz)
      .then((u) => {
        setUsage(u);
        setUsageError(null);
      })
      .catch((e: unknown) => setUsageError(errorText(e)));
  }, [range, tz]);

  usePoll(loadLive, LIVE_MS, [loadLive]);
  usePoll(loadUsage, USAGE_MS, [loadUsage]);

  const toggle = (key: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      writeHidden(next);
      return next;
    });
  };

  return (
    <div className="screen">
      <div className="screen-inner wide ctx">
        <div className="section-head">
          <h1>Context</h1>
          <div className="row">
            <span className="small muted">{tz}</span>
            <IconButton
              icon="refresh"
              label="Refresh"
              onClick={() => {
                loadLive();
                loadUsage();
              }}
            />
          </div>
        </div>

        <section aria-labelledby="ctx-now">
          <h2 id="ctx-now">Now</h2>
          {liveError && <div className="card ep bad">Live view unavailable: {liveError}</div>}
          {!live && !liveError && <div className="muted small">Loading…</div>}
          {live && (
            <>
              <div className="ctx-endpoints">
                {live.endpoints.map((e) => (
                  <LiveBar key={e.endpoint + e.model} ep={e} recentS={live.recent_s} />
                ))}
              </div>
              <OverflowSummary counts={live.overflows_24h} items={live.last_overflows} tz={tz} />
            </>
          )}
        </section>

        <section aria-labelledby="ctx-history">
          <div className="section-head ctx-head">
            <h2 id="ctx-history">Over time</h2>
            <div className="seg ctx-seg-ctl" role="tablist" aria-label="Range">
              {RANGES.map((r) => (
                <button key={r.id} type="button" role="tab" aria-selected={range === r.id} className={range === r.id ? 'active' : ''} onClick={() => setRange(r.id)}>
                  {r.label}
                </button>
              ))}
            </div>
          </div>
          <div className="ctx-metrics">
            <div className="seg ctx-seg-ctl" role="tablist" aria-label="Metric">
              {METRICS.map((m) => (
                <button key={m.id} type="button" role="tab" aria-selected={metric === m.id} className={metric === m.id ? 'active' : ''} onClick={() => setMetric(m.id)} title={m.hint}>
                  {m.label}
                </button>
              ))}
            </div>
            <span className="small muted">{METRICS.find((m) => m.id === metric)?.hint}</span>
          </div>
          {usageError && <div className="card ep bad">History unavailable: {usageError}</div>}
          {usage && (
            <>
              <Legend usage={usage} hidden={hidden} onToggle={toggle} />
              <UsageChart usage={usage} metric={metric} hidden={hidden} />
              <SourceTable usage={usage} />
              <HourProfile usage={usage} hidden={hidden} />
            </>
          )}
        </section>
      </div>
    </div>
  );
}

// --- now ------------------------------------------------------------------------------

function LiveBar({ ep, recentS }: { ep: LiveEndpoint; recentS: number }) {
  const running = ep.calls.filter((c) => c.state === 'running');
  const waiting = ep.calls.filter((c) => c.state === 'waiting');
  // Unknown window: scale to what is there, so the bar still compares the segments.
  const scale = ep.window ?? Math.max(ep.used, ep.recent_peak, 1);
  const over = ep.window !== null && ep.used > ep.window;
  const usedPct = pctOf(ep.used, ep.window);
  const recentPct = pctOf(ep.recent_peak, ep.window);
  const busyPct = (ep.recent_busy_ms / (recentS * 1000)) * 100;
  return (
    <div className={`card ctx-ep${over ? ' over' : ''}`}>
      <div className="ctx-ep-head">
        <div className="truncate">
          <strong>{ep.model || 'model'}</strong>
          <span className="small muted"> · {endpointName(ep.endpoint)}</span>
        </div>
        <div className="small">
          {running.length ? (
            <>
              <strong>{fmtTokens(ep.used)}</strong>
              {ep.window ? ` / ${fmtTokens(ep.window)} (${fmtPct(usedPct)})` : ' tokens (window unknown)'}
            </>
          ) : (
            <span className="muted">idle{ep.window ? ` · window ${fmtTokens(ep.window)}` : ''}</span>
          )}
        </div>
      </div>
      <div
        className="ctx-bar"
        role="meter"
        aria-label={`Context in use on ${ep.model}`}
        aria-valuemin={0}
        aria-valuemax={scale}
        aria-valuenow={Math.min(ep.used, scale)}
      >
        {running.map((c) => (
          <span
            key={c.id}
            className="ctx-seg"
            style={{ width: `${Math.min(100, (c.tokens / scale) * 100)}%`, background: colorFor(c.key) }}
            title={`${sourceName(c.key)}${c.role ? ` (${c.role})` : ''}: ${fmtTokens(c.tokens)} tokens`}
          />
        ))}
        {ep.window !== null && ep.recent_peak > 0 && (
          <span className="ctx-mark" style={{ left: `${Math.min(100, (ep.recent_peak / scale) * 100)}%` }} title={`Peak in the last ${recentS / 60} min: ${fmtTokens(ep.recent_peak)}`} />
        )}
      </div>
      <div className="ctx-ep-foot small">
        {running.map((c) => (
          <span key={c.id} className="ctx-tag">
            <i style={{ background: colorFor(c.key) }} />
            {sourceName(c.key)}
            {c.role && c.role !== 'chat' && c.role !== 'background' ? <span className="muted"> · {c.role}</span> : null} {fmtTokens(c.tokens)}
            {c.trimmed_tokens > 0 && <span className="chip chip-warn">trimmed {fmtTokens(c.trimmed_tokens)}</span>}
          </span>
        ))}
        {waiting.length > 0 && (
          <span className="chip chip-info" title={waiting.map((c) => sourceName(c.key)).join(', ')}>
            {waiting.length} queued
          </span>
        )}
        {over && <span className="chip chip-danger">over the window</span>}
        <span className="muted ctx-recent">
          last {recentS / 60} min: peak {fmtTokens(ep.recent_peak)}
          {recentPct !== null ? ` (${fmtPct(recentPct)})` : ''} · busy {fmtPct(busyPct)}
        </span>
      </div>
      <div className="small muted">roles: {ep.roles.join(', ') || '—'}</div>
    </div>
  );
}

function OverflowSummary({ counts, items, tz }: { counts: Record<OverflowKind, number>; items: OverflowItem[]; tz: string }) {
  const total = OVERFLOW_ORDER.reduce((n, k) => n + counts[k], 0);
  return (
    <details className="card ctx-overflows" open={counts.rejected > 0}>
      <summary>
        <span>Overflows, last 24 h</span>
        {OVERFLOW_ORDER.map((k) => (
          <span key={k} className={`chip ${counts[k] ? (k === 'rejected' ? 'chip-danger' : 'chip-warn') : ''}`} title={OVERFLOW_LABELS[k].hint}>
            {OVERFLOW_LABELS[k].label} {counts[k]}
          </span>
        ))}
      </summary>
      {total === 0 ? <p className="small muted">None. Every call fitted its window.</p> : <OverflowList items={items} tz={tz} />}
    </details>
  );
}

function OverflowList({ items, tz }: { items: OverflowItem[]; tz: string }) {
  const openConversation = useStore((s) => s.openConversation);
  return (
    <ul className="ctx-ovl">
      {items.map((o) => (
        <li key={`${o.at}-${o.key}-${o.kinds.join()}`}>
          <span className="muted">{fmtTime(o.at, tz)}</span>
          <span className="ctx-tag">
            <i style={{ background: colorFor(o.key) }} />
            {sourceName(o.key)}
          </span>
          {o.kinds.map((k) => (
            <span key={k} className={`chip ${k === 'rejected' ? 'chip-danger' : 'chip-warn'}`}>
              {OVERFLOW_LABELS[k].label}
            </span>
          ))}
          <span>
            {fmtTokens(o.tokens)}
            {o.window ? ` / ${fmtTokens(o.window)}` : ''}
            {o.trimmed_tokens ? ` · dropped ${fmtTokens(o.trimmed_tokens)}` : ''}
          </span>
          {o.conversation_id !== null && (
            <button
              type="button"
              className="linklike small"
              onClick={() => void openConversation(o.conversation_id)}
            >
              open
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

// --- over time --------------------------------------------------------------------------

function visibleKeys(usage: ContextUsage, hidden: Set<string>): string[] {
  return Object.keys(usage.series)
    .filter((k) => !hidden.has(k))
    .sort((a, b) => sourceName(a).localeCompare(sourceName(b)));
}

function Legend({ usage, hidden, onToggle }: { usage: ContextUsage; hidden: Set<string>; onToggle: (k: string) => void }) {
  const keys = Object.keys(usage.series).sort((a, b) => sourceName(a).localeCompare(sourceName(b)));
  if (!keys.length) return <p className="small muted">No model calls in this range yet.</p>;
  return (
    <div className="ctx-legend" role="group" aria-label="Sources">
      {keys.map((k) => (
        <button key={k} type="button" className={`chip chip-toggle${hidden.has(k) ? '' : ' active'}`} aria-pressed={!hidden.has(k)} onClick={() => onToggle(k)} title={sourceKind(k)}>
          <i style={{ background: hidden.has(k) ? 'transparent' : colorFor(k), borderColor: colorFor(k) }} />
          {sourceName(k)}
        </button>
      ))}
    </div>
  );
}

const H = 240;
const PAD = { l: 48, r: 12, t: 12, b: 28 };

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(([entry]) => setW(Math.max(240, Math.floor(entry.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function UsageChart({ usage, metric, hidden }: { usage: ContextUsage; metric: Metric; hidden: Set<string> }) {
  const [wrap, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const keys = visibleKeys(usage, hidden);
  const n = usage.buckets.length;
  const lines = keys.map((k) => ({ key: k, values: seriesValues(usage.series[k], metric, usage.lengths) }));
  const windows = metric === 'peak' ? Object.entries(usage.windows) : [];
  const totals =
    metric === 'busy'
      ? Object.entries(usage.endpoint_busy).map(([ep, v]) => ({ ep, values: v.map((b, i) => Math.min(100, (b / Math.max(1, usage.lengths[i])) * 100)) }))
      : [];
  const overflowAt = usage.buckets.map((_, i) => keys.reduce((s, k) => s + (usage.series[k].overflows[i] ?? 0), 0));
  let top = 0;
  for (const l of lines) for (const v of l.values) top = Math.max(top, v);
  for (const [, w] of windows) for (const v of w) top = Math.max(top, v ?? 0);
  const yMax = metric === 'busy' ? 100 : niceMax(top * 1.05);
  const iw = width - PAD.l - PAD.r;
  const ih = H - PAD.t - PAD.b;
  const x = (i: number) => PAD.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v: number) => PAD.t + ih - (Math.min(v, yMax) / yMax) * ih;
  const path = (vals: (number | null)[]) => vals.map((v, i) => (v === null ? '' : `${i === 0 || vals[i - 1] === null ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join('');
  const fmtY = (v: number) => (metric === 'busy' ? `${Math.round(v)}%` : fmtTokens(v));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const labelEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 70))));

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left - PAD.l;
    setHover(n <= 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round((px / iw) * (n - 1)))));
  };

  if (!n) return null;
  return (
    <div className="card ctx-chart" ref={wrap}>
      <svg width={width} height={H} role="img" aria-label={`${METRICS.find((m) => m.id === metric)?.label} over ${usage.range}`} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} className="ctx-grid" />
            <text x={PAD.l - 6} y={y(t) + 4} textAnchor="end" className="ctx-axis">
              {fmtY(t)}
            </text>
          </g>
        ))}
        {usage.buckets.map((b, i) =>
          i % labelEvery === 0 ? (
            <text key={b} x={x(i)} y={H - 8} textAnchor="middle" className="ctx-axis">
              {fmtBucket(b, usage.bucket_s, usage.tz)}
            </text>
          ) : null,
        )}
        {windows.map(([ep, w]) => (
          <g key={ep}>
            <path d={path(w)} className="ctx-window" />
          </g>
        ))}
        {totals.map((t) => (
          <path key={t.ep} d={path(t.values)} className="ctx-total" />
        ))}
        {lines.map((l) => (
          <g key={l.key}>
            <path d={path(l.values)} fill="none" stroke={colorFor(l.key)} strokeWidth={2} strokeLinejoin="round" />
            {l.values.map((v, i) => (usage.series[l.key].calls[i] > 0 ? <circle key={i} cx={x(i)} cy={y(v)} r={2.5} fill={colorFor(l.key)} /> : null))}
          </g>
        ))}
        {overflowAt.map((c, i) => (c > 0 ? <circle key={i} cx={x(i)} cy={PAD.t + ih} r={Math.min(7, 3 + c)} className="ctx-ovf-dot" /> : null))}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={PAD.t + ih} className="ctx-cross" />}
      </svg>
      {hover !== null && (
        <div className="ctx-tip" style={{ left: Math.min(width - 220, Math.max(0, x(hover) + 10)) }}>
          <div className="small">
            <strong>{fmtBucket(usage.buckets[hover], usage.bucket_s, usage.tz)}</strong>
          </div>
          {lines
            .filter((l) => usage.series[l.key].calls[hover] > 0 || l.values[hover] > 0)
            .map((l) => (
              <div key={l.key} className="small ctx-tip-row">
                <i style={{ background: colorFor(l.key) }} />
                <span className="truncate">{sourceName(l.key)}</span>
                <span>{fmtY(l.values[hover])}</span>
                <span className="muted">{usage.series[l.key].calls[hover]} calls</span>
              </div>
            ))}
          {windows.map(([ep, w]) => (
            <div key={ep} className="small muted">
              window {endpointName(ep)}: {fmtTokens(w[hover])}
            </div>
          ))}
          {totals.map((t) => (
            <div key={t.ep} className="small muted">
              {endpointName(t.ep)} busy: {fmtPct(t.values[hover])}
            </div>
          ))}
          {overflowAt[hover] > 0 && <div className="small ctx-danger">{overflowAt[hover]} overflow(s)</div>}
        </div>
      )}
      {usage.overflow_count > 0 && (
        <details className="ctx-chart-ovl">
          <summary className="small">
            {usage.overflow_count} overflow{usage.overflow_count === 1 ? '' : 's'} in this range (red dots)
          </summary>
          <OverflowList items={usage.overflows} tz={usage.tz} />
        </details>
      )}
    </div>
  );
}

function SourceTable({ usage }: { usage: ContextUsage }) {
  if (!usage.table.length) return null;
  const span = usage.lengths.reduce((a, b) => a + b, 0);
  return (
    <div className="card ctx-table-wrap">
      <table className="ctx-table">
        <thead>
          <tr>
            <th>Source</th>
            <th title="Model calls in the range">Calls</th>
            <th title="Typical (median) context per call">Typical</th>
            <th title="95th percentile context per call">p95</th>
            <th title="Largest single call, and its share of the window">Peak</th>
            <th title="Time the model worked for it, and share of the range">Busy</th>
            <th title="Prompt tokens read (not cached) + written">Processed</th>
            <th>Overflows</th>
          </tr>
        </thead>
        <tbody>
          {usage.table.map((r) => {
            const ov = OVERFLOW_ORDER.filter((k) => r.overflows[k] > 0);
            const peakPct = pctOf(r.peak, r.max_window);
            return (
              <tr key={r.key}>
                <td>
                  <span className="ctx-tag">
                    <i style={{ background: colorFor(r.key) }} />
                    {sourceName(r.key)}
                  </span>
                </td>
                <td>
                  {r.calls}
                  {r.failed ? <span className="muted"> ({r.failed} failed)</span> : null}
                </td>
                <td>{fmtTokens(r.p50)}</td>
                <td>{fmtTokens(r.p95)}</td>
                <td className={peakPct !== null && peakPct >= 90 ? 'ctx-danger' : peakPct !== null && peakPct >= 70 ? 'ctx-warn' : ''}>
                  {fmtTokens(r.peak)} <span className="muted">{fmtPct(peakPct)}</span>
                </td>
                <td>
                  {fmtMinutes(r.busy_ms)} <span className="muted">{fmtPct((r.busy_ms / Math.max(1, span)) * 100)}</span>
                </td>
                <td>{fmtTokens(r.tokens)}</td>
                <td>
                  {ov.length === 0 ? (
                    <span className="muted">—</span>
                  ) : (
                    ov.map((k) => (
                      <span key={k} className={`chip ${k === 'rejected' ? 'chip-danger' : 'chip-warn'}`} title={OVERFLOW_LABELS[k].hint}>
                        {OVERFLOW_LABELS[k].label} {r.overflows[k]}
                      </span>
                    ))
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function HourProfile({ usage, hidden }: { usage: ContextUsage; hidden: Set<string> }) {
  const prof = usage.hour_profile.busy_min_per_day;
  const keys = Object.keys(prof)
    .filter((k) => !hidden.has(k))
    .sort((a, b) => sourceName(a).localeCompare(sourceName(b)));
  if (!keys.length) return null;
  const visible = Object.fromEntries(keys.map((k) => [k, prof[k]]));
  const totals = hourTotals(visible);
  const max = Math.max(1, ...totals);
  const quiet = quietestHours(totals, 3);
  return (
    <div className="card ctx-hours">
      <div className="ctx-ep-head">
        <strong>Typical day</strong>
        <span className="small muted">
          busy minutes per hour, average of the last {usage.hour_profile.days} days · quietest: {quiet.map((h) => `${String(h).padStart(2, '0')}:00`).join(', ')}
        </span>
      </div>
      <div className="ctx-hour-cols" role="img" aria-label="Busy minutes per hour of day">
        {totals.map((t, h) => (
          <div key={h} className={`ctx-hour${quiet.includes(h) ? ' quiet' : ''}`} title={`${String(h).padStart(2, '0')}:00 · ${t.toFixed(1)} min/day busy`}>
            <div className="ctx-hour-stack" style={{ height: `${(t / max) * 100}%` }}>
              {keys.map((k) =>
                prof[k][h] > 0 ? <span key={k} style={{ flexGrow: prof[k][h], background: colorFor(k) }} title={`${sourceName(k)}: ${prof[k][h].toFixed(1)} min/day`} /> : null,
              )}
            </div>
            <span className="ctx-hour-label">{h % 3 === 0 ? String(h).padStart(2, '0') : ''}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
