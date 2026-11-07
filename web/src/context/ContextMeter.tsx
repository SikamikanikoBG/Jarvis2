import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { ContextLive } from '../protocol/types';
import { useStore } from '../store/store';
import { colorFor, fmtPct, fmtTokens, pctOf, sourceName } from './contextData';

const POLL_MS = 5_000;

/** The top bar's glance: the fullest endpoint's context in use now, coloured by who holds it,
 *  and a red dot when anything was refused in the last 24 hours. Opens the Context screen. */
export function ContextMeter() {
  const setView = useStore((s) => s.setView);
  const [live, setLive] = useState<ContextLive | null>(null);
  const load = useCallback(() => {
    api.context
      .live()
      .then(setLive)
      .catch(() => setLive(null));
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') load();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [load]);
  if (!live?.endpoints.length) return null;
  const ep = [...live.endpoints].sort((a, b) => (pctOf(b.used, b.window) ?? 0) - (pctOf(a.used, a.window) ?? 0))[0];
  const scale = ep.window ?? Math.max(ep.used, 1);
  const running = ep.calls.filter((c) => c.state === 'running');
  const waiting = live.endpoints.reduce((n, e) => n + e.waiting, 0);
  const refused = live.overflows_24h.rejected;
  const pct = pctOf(ep.used, ep.window);
  const title = [
    running.length ? `${ep.model}: ${fmtTokens(ep.used)}${ep.window ? ` of ${fmtTokens(ep.window)} (${fmtPct(pct)})` : ''}` : `${ep.model}: idle`,
    ...running.map((c) => `· ${sourceName(c.key)} ${fmtTokens(c.tokens)}`),
    waiting ? `${waiting} queued` : '',
    refused ? `${refused} refused as too long in 24 h` : '',
    'Open the Context screen',
  ]
    .filter(Boolean)
    .join('\n');
  return (
    <button type="button" className="ctx-meter" onClick={() => setView('context')} title={title} aria-label={title}>
      <span className="ctx-meter-bar">
        {running.map((c) => (
          <span key={c.id} style={{ width: `${Math.min(100, (c.tokens / scale) * 100)}%`, background: colorFor(c.key) }} />
        ))}
      </span>
      <span className="ctx-meter-num">{running.length ? fmtPct(pct) : 'idle'}</span>
      {waiting > 0 && <span className="ctx-meter-q">+{waiting}</span>}
      {refused > 0 && <span className="dot dot-danger" />}
    </button>
  );
}
