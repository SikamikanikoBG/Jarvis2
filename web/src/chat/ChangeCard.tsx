import { useState } from 'react';
import { ApiError, api, describeError } from '../api/client';
import { Icon } from '../components/Icon';
import { formatRelative, previewValue } from '../lib/format';
import type { ChangeField, ChangeRecord, ChangeStatus } from '../protocol/types';
import { useStore } from '../store/store';

const STATUS: Record<ChangeStatus, { label: string; chip: string }> = {
  applied: { label: 'applied', chip: 'chip-ok' },
  probing: { label: 'checking…', chip: 'chip-info' },
  proposed: { label: 'waits for you', chip: 'chip-warn' },
  auto_reverted: { label: 'put back: did not answer', chip: 'chip-danger' },
  reverted: { label: 'reverted', chip: 'chip' },
  discarded: { label: 'discarded', chip: 'chip' },
  expired: { label: 'lapsed', chip: 'chip' },
};

function shown(value: unknown, secret: boolean): string {
  if (value === null || value === undefined) return '—';
  if (secret && typeof value === 'string') return value || '—';
  return previewValue(value, 140);
}

/** old → new per field; the core masks secrets before they get here. */
export function ChangeDiff({ fields }: { fields: ChangeField[] }) {
  return (
    <div className="change-diff" role="table" aria-label="What changes">
      {fields.map((f) => (
        <div key={`${f.op}:${f.path}`} className="change-row" role="row">
          <span className="change-path mono" role="cell">
            {f.op === 'add' ? '+ ' : f.op === 'remove' ? '− ' : ''}
            {f.path}
          </span>
          <span className="change-vals" role="cell">
            {f.op !== 'add' && <s className="change-old">{shown(f.old, f.secret)}</s>}
            {f.op === 'set' && <span className="muted"> → </span>}
            {f.op !== 'remove' && <span className="change-new">{shown(f.new, f.secret)}</span>}
          </span>
        </div>
      ))}
    </div>
  );
}

function title(change: ChangeRecord): string {
  if (change.area === 'settings') return 'Setting';
  if (change.area === 'schedule') return 'Scheduled prompt';
  return change.target;
}

/**
 * What a tool call changed, under its tool card: old → new, and Revert while it is in force, or
 * Apply / Discard for a change a run nobody watched proposed (docs/stories/14_agent_admin.md).
 * The latest status comes from `change.recorded`, so a revert from Settings shows here at once.
 */
export function ChangeCard({ change: initial }: { change: ChangeRecord }) {
  const live = useStore((s) => s.changes[initial.id]);
  const change = live ?? initial;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const status = STATUS[change.status] ?? STATUS.applied;
  const writtenByRun = change.area === 'schedule' && change.status === 'applied' && change.fields.some((f) => f.path === 'prompt_by' && f.new);

  const act = async (what: 'revert' | 'apply' | 'discard' | 'accept') => {
    setBusy(true);
    setError(null);
    try {
      if (what === 'accept') await api.schedules.accept(change.target);
      else await api.settings[what](change.id);
    } catch (e) {
      setError(e instanceof ApiError ? describeError(e.status, e.body) : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`change-card${change.status === 'proposed' ? ' proposed' : ''}`} role="group" aria-label={`Change ${change.id}`}>
      <div className="change-head">
        <Icon name={change.area === 'schedule' ? 'clock' : change.area === 'api' ? 'server' : 'sliders'} size={15} />
        <span className="change-title">{title(change)}</span>
        <span className={`chip ${status.chip}`}>{status.label}</span>
        {change.risk.length > 0 && <span className="chip chip-outline">{change.risk.join(' · ')}</span>}
        <span className="muted small change-when" title={change.at}>
          {formatRelative(change.at)}
        </span>
      </div>
      {change.fields.length > 0 && <ChangeDiff fields={change.fields.filter((f) => f.path !== 'prompt_by')} />}
      {(change.reason || change.note) && <div className="small muted">{[change.reason, change.note].filter(Boolean).join(' — ')}</div>}
      {change.status === 'applied' && change.area === 'settings' && <div className="field-hint">Anything it already did while it was on stays done.</div>}
      {writtenByRun && <div className="field-hint">Written by a run, not by you: it asks before acting like a task until you accept it.</div>}
      <div className="change-actions">
        {change.revertible && (
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void act('revert')}>
            <Icon name="refresh" size={14} /> Revert
          </button>
        )}
        {change.status === 'proposed' && (
          <>
            <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => void act('apply')}>
              <Icon name="check" size={14} /> Apply
            </button>
            <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void act('discard')}>
              <Icon name="x" size={14} /> Discard
            </button>
          </>
        )}
        {writtenByRun && (
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void act('accept')}>
            <Icon name="check" size={14} /> Accept prompt
          </button>
        )}
        {error && <span className="msg-err small">{error}</span>}
      </div>
    </div>
  );
}
