import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton, InlineConfirm, RelativeTime } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import type { CollabKeyCreated } from '../protocol/types';
import { useStore } from '../store/store';

/** Collaborator keys for `/mcp` and `/api/collab/message`. The key is shown exactly once. */
export function CollabSection() {
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.collab.keys(), []);
  const { data, error, reload } = useLoader(load, 'keys');
  const [name, setName] = useState('');
  const [created, setCreated] = useState<CollabKeyCreated | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const create = () => {
    const n = name.trim();
    if (!n) return;
    setBusy(true);
    api.collab
      .createKey(n)
      .then((k) => {
        setCreated(k);
        setName('');
        reload();
      })
      .catch((e: unknown) => notify(`Could not create the key: ${errorText(e)}`, 'error'))
      .finally(() => setBusy(false));
  };
  const remove = (id: string) =>
    api.collab
      .removeKey(id)
      .then(() => {
        setConfirmDelete(null);
        reload();
      })
      .catch((e: unknown) => notify(`Delete failed: ${errorText(e)}`, 'error'));
  const copy = (text: string) => navigator.clipboard.writeText(text).then(() => notify('Copied.')).catch(() => notify('Copy failed — select the text manually.', 'error'));

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Collaborators</h2>
        <p>Bearer keys for other agents (MCP at /mcp, or POST /api/collab/message). Each key gets its own Collab folder.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      {created && (
        <div className="key-reveal" role="status">
          <div className="small">
            Key for <b>{created.name}</b> — copy it now, it will not be shown again.
          </div>
          <div className="row">
            <code className="key-code mono">{created.key}</code>
            <IconButton icon="copy" label="Copy key" onClick={() => void copy(created.key)} />
            <IconButton icon="x" label="Hide" onClick={() => setCreated(null)} />
          </div>
        </div>
      )}
      <div className="mcp-list">
        {data?.length === 0 && <div className="empty small">No collaborator keys.</div>}
        {(data ?? []).map((k) => (
          <div key={k.id} className="mcp-row">
            <Icon name="shield" size={16} className="muted" />
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="row">
                <span style={{ fontWeight: 600 }}>{k.name}</span>
                <span className="xs muted mono">{k.id}</span>
              </div>
              <div className="xs muted">
                created <RelativeTime ts={k.created_at} /> · {k.last_used_at ? <>last used <RelativeTime ts={k.last_used_at} /></> : 'never used'}
              </div>
            </div>
            {confirmDelete === k.id ? (
              <InlineConfirm text={`Revoke "${k.name}"?`} confirmLabel="Revoke" danger onConfirm={() => void remove(k.id)} onCancel={() => setConfirmDelete(null)} />
            ) : (
              <IconButton icon="trash" label={`Revoke ${k.name}`} size="sm" onClick={() => setConfirmDelete(k.id)} />
            )}
          </div>
        ))}
        <div className="row">
          <input
            className="input"
            style={{ maxWidth: 280 }}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                create();
              }
            }}
            placeholder="Collaborator name (e.g. claude-code)"
            aria-label="Key name"
          />
          <button type="button" className="btn btn-secondary btn-sm" onClick={create} disabled={!name.trim() || busy}>
            <Icon name="plus" size={14} />
            Create key
          </button>
        </div>
      </div>
    </section>
  );
}
