import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton, RelativeTime } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import { useStore } from '../store/store';

/** Per-account triage cursor state and a manual trigger. Settings live in Settings → Triage. */
export function TriageScreen() {
  const notify = useStore((s) => s.notify);
  const setView = useStore((s) => s.setView);
  const load = useCallback(() => api.triage.state(), []);
  const { data, error, loading, reload } = useLoader(load, 'triage');
  const [running, setRunning] = useState(false);

  const runNow = () => {
    setRunning(true);
    api.triage
      .run()
      .then((r) => {
        notify(`Triage started (${r.run_id}).`);
        setTimeout(reload, 1500);
      })
      .catch((e: unknown) => notify(`Triage failed to start: ${errorText(e)}`, 'error'))
      .finally(() => setRunning(false));
  };

  return (
    <div className="screen">
      <div className="screen-inner">
        <div className="section-head">
          <h1>Triage</h1>
          <div className="row">
            <IconButton icon="refresh" label="Refresh" onClick={reload} />
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setView('settings')}>
              <Icon name="sliders" size={14} />
              Settings
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={runNow} disabled={running}>
              <Icon name="play" size={14} />
              {running ? 'Starting…' : 'Run now'}
            </button>
          </div>
        </div>
        <p className="small muted" style={{ margin: 0 }}>
          Mail is polled from the host by cursor, demand numbers are routed deterministically, the rest is classified by the triage role. One conversation per account per day under Triage.
        </p>
        {error && <div className="field-error">{error}</div>}
        {!loading && data?.length === 0 && (
          <div className="empty">
            <strong>Triage has not run yet</strong>
            <span>Enable it and pick the host and accounts in Settings, or press Run now.</span>
          </div>
        )}
        {data && data.length > 0 && (
          <div className="budget-wrap card" style={{ padding: 0 }}>
            <table className="budget-table triage-table">
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Day</th>
                  <th>Processed</th>
                  <th>Routed</th>
                  <th>Last run</th>
                  <th>Cursor</th>
                </tr>
              </thead>
              <tbody>
                {data.map((s) => (
                  <tr key={s.account} className={s.last_error ? 'has-error' : ''}>
                    <td>{s.account}</td>
                    <td className="mono">{s.day ?? '—'}</td>
                    <td className="mono">{s.processed_today}</td>
                    <td className="mono">{s.routed_today}</td>
                    <td>{s.last_run_at ? <RelativeTime ts={s.last_run_at} /> : '—'}</td>
                    <td className="mono xs truncate" style={{ maxWidth: 180 }} title={s.cursor ?? ''}>
                      {s.cursor ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.some((s) => s.last_error) && (
              <div style={{ padding: '8px 12px' }}>
                {data
                  .filter((s) => s.last_error)
                  .map((s) => (
                    <div key={s.account} className="field-error">
                      {s.account}: {s.last_error}
                    </div>
                  ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
