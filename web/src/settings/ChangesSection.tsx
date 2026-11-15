import { useEffect, useState } from 'react';
import { ApiError, api, describeError } from '../api/client';
import { ChangeCard } from '../chat/ChangeCard';
import type { ChangeRecord } from '../protocol/types';
import { useStore } from '../store/store';

const FIRST = 5;

/**
 * The trail (docs/stories/14_agent_admin.md): what changed in the settings, the schedules and
 * through the routes, by Arsen or by Jarvis, newest first - with Undo for a settings or schedule
 * change and Apply for one a run proposed. It needs no model: with both lanes down this is how a
 * bad change Jarvis made is taken back from the phone.
 */
export function ChangesSection() {
  const [items, setItems] = useState<ChangeRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const version = useStore((s) => s.featureVersion.changes);

  useEffect(() => {
    let live = true;
    api.settings
      .changes({ limit: 40 })
      .then((list) => {
        if (live) setItems(list);
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof ApiError ? describeError(e.status, e.body) : String(e));
      });
    return () => {
      live = false;
    };
  }, [version]);

  const waiting = (items ?? []).filter((c) => c.status === 'proposed');
  const rest = (items ?? []).filter((c) => c.status !== 'proposed');
  const shown = all ? rest : rest.slice(0, FIRST);
  const last = rest.find((c) => c.revertible);

  return (
    <section id="settings-changes" className="card role-card settings-anchor">
      <div className="section-head">
        <h2>Changes</h2>
        <p>Who changed what, by hand or by Jarvis. Undo works with every model down.</p>
      </div>
      {error && <div className="msg-err">{error}</div>}
      {items === null && !error && <div className="muted small">Loading…</div>}
      {items !== null && items.length === 0 && <div className="muted small">Nothing has changed yet.</div>}
      {last && (
        <div className="change-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => {
              void api.settings.revert(last.id).catch((e: unknown) => setError(e instanceof ApiError ? describeError(e.status, e.body) : String(e)));
            }}
          >
            Undo last change
          </button>
          <span className="small muted">{last.fields.map((f) => f.path).join(', ')}</span>
        </div>
      )}
      {waiting.length > 0 && (
        <>
          <div className="label">Waiting for you</div>
          {waiting.map((c) => (
            <ChangeCard key={c.id} change={c} />
          ))}
        </>
      )}
      {shown.map((c) => (
        <ChangeCard key={c.id} change={c} />
      ))}
      {rest.length > FIRST && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAll((v) => !v)}>
          {all ? 'Show fewer' : `Show all ${rest.length}`}
        </button>
      )}
    </section>
  );
}
