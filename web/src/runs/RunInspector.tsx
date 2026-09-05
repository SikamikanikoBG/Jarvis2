import { useMemo, useState } from 'react';
import { IconButton } from '../components/primitives';
import { useTicker } from '../components/useTicker';
import { formatClock, formatDuration, formatTokens, prettyJson } from '../lib/format';
import { runDurationMs, statusChipClass } from '../lib/runs';
import { describeThink } from '../lib/think';
import { isTerminal } from '../protocol/types';
import { useStore } from '../store/store';
import { buildTimeline, type TimelineRow } from './timeline';

const EMPTY: never[] = [];

export function RunInspector({ runId }: { runId: string }) {
  const run = useStore((s) => s.runs[runId]);
  const events = useStore((s) => s.runEvents[runId] ?? EMPTY);
  const loaded = useStore((s) => s.runEventsLoaded[runId] === true);
  const close = useStore((s) => s.openInspector);
  const active = run ? !isTerminal(run.status) : false;
  const now = useTicker(1000, active);
  const rows = useMemo(() => buildTimeline(events), [events]);

  const dur = run ? runDurationMs(run, now) : null;
  const statusClass = run ? statusChipClass(run.status, active) : 'chip-outline';

  return (
    <div className="inspector" aria-label="Run inspector">
      <div className="inspector-head">
        <h2 className="truncate">Run</h2>
        {run && <span className="chip chip-outline">{run.kind}</span>}
        {run && (
          <span className={`chip ${statusClass}`}>
            {active && <span className="dot dot-accent dot-pulse" />}
            {run.status.replace('_', ' ')}
          </span>
        )}
        {run && <span className="chip chip-outline">{describeThink(run.think, run.think_level) ?? 'think · role default'}</span>}
        <IconButton icon="x" label="Close inspector" onClick={() => close(null)} />
      </div>
      <div className="inspector-body">
        {run && (
          <>
            <div className="run-summary">
              <Stat k="steps" v={`${run.steps_used}${run.budget.max_steps ? ` / ${run.budget.max_steps}` : ''}`} />
              <Stat k="prompt" v={formatTokens(run.usage.prompt_tokens)} />
              <Stat k="completion" v={formatTokens(run.usage.completion_tokens)} />
              <Stat k="duration" v={dur !== null ? formatDuration(dur) : '—'} />
              <Stat k="model calls" v={String(run.usage.calls)} />
              <Stat k="TTFT" v={run.usage.ttft_ms !== null ? formatDuration(run.usage.ttft_ms) : '—'} />
            </div>
            {run.input_text && <div className="run-input">{run.input_text}</div>}
            {run.error && <div className="field-error" style={{ marginBottom: 12 }}>{run.error}</div>}
          </>
        )}
        {rows.length === 0 ? (
          <div className="empty small">{loaded ? 'No events recorded for this run.' : 'Loading events…'}</div>
        ) : (
          <div className="timeline" role="list">
            {rows.map((r) => (
              <TimelineItem key={r.key} row={r} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="stat">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}

function TimelineItem({ row }: { row: TimelineRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="tl" role="listitem">
      <div className="tl-time">
        {formatClock(row.ts, true)}
        <span className="tl-delta">{row.deltaMs === null ? '' : `+${formatDuration(row.deltaMs)}`}</span>
      </div>
      <div className="tl-main">
        <button type="button" className="tl-head" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className={`tl-type ${row.category}`}>{row.type}</span>
          <span className="tl-preview">{row.preview}</span>
        </button>
        {row.metrics.length > 0 && (
          <div className="tl-metrics">
            {row.metrics.map((m) => (
              <span key={m.label}>
                {m.label} <b>{m.value}</b>
              </span>
            ))}
          </div>
        )}
        {open && <pre className="tl-payload">{prettyJson(row.payload)}</pre>}
      </div>
    </div>
  );
}
