import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, api, describeError } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { formatDuration } from '../lib/format';
import type { EndpointStatus, StatusResponse, ToolProviderStatus, ToolSpec } from '../protocol/types';
import { useStore } from '../store/store';
import { PairPanel } from './PairPanel';

const REFRESH_MS = 30_000;

const errText = (e: unknown) => (e instanceof ApiError ? describeError(e.status, e.body) : String(e));

/** Honest instrument panel: what is reachable right now. A red card stays red. */
export function StatusScreen() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [tools, setTools] = useState<ToolSpec[] | null>(null);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);
  const connection = useStore((s) => s.connection);
  const attempt = useStore((s) => s.connectionAttempt);
  const version = useStore((s) => s.version);
  const toolsVersion = useStore((s) => s.featureVersion.tools);

  // State updates only happen in the promise callbacks (asynchronously), never in the effect body itself.
  const load = useCallback(
    () =>
      api
        .status()
        .then((s) => {
          setStatus(s);
          setError(null);
        })
        .catch((e: unknown) => setError(errText(e)))
        .finally(() => setCheckedAt(Date.now())),
    [],
  );
  const loadTools = useCallback(
    () =>
      api.tools
        .list()
        .then((t) => {
          setTools(t);
          setToolsError(null);
        })
        .catch((e: unknown) => setToolsError(errText(e))),
    [],
  );

  const refresh = () => {
    setBusy(true);
    void Promise.all([load(), loadTools()]).finally(() => setBusy(false));
  };

  const reloadTools = () => {
    setReloading(true);
    api.tools
      .reload()
      .then((t) => {
        setTools(t);
        setToolsError(null);
        return load();
      })
      .catch((e: unknown) => setToolsError(errText(e)))
      .finally(() => setReloading(false));
  };

  // `toolsVersion` bumps on `tools.changed`, which refetches both lists.
  useEffect(() => {
    void load();
    void loadTools();
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') void load();
    }, REFRESH_MS);
    return () => clearInterval(id);
  }, [load, loadTools, toolsVersion]);

  const connLabel = connection === 'open' ? 'connected' : connection === 'connecting' ? 'connecting' : connection === 'reconnecting' ? `reconnecting (try ${attempt})` : 'closed';

  return (
    <div className="screen">
      <div className="screen-inner">
        <div className="section-head">
          <h1>Status</h1>
          <div className="row">
            {checkedAt && <span className="small muted">checked {new Date(checkedAt).toLocaleTimeString()}</span>}
            <IconButton icon="refresh" label="Refresh" onClick={refresh} disabled={busy} />
          </div>
        </div>

        <PairPanel />

        <div className="status-row">
          <div className="stat">
            <div className="k">core</div>
            <div className="v">{status?.version ?? version ?? '—'}</div>
          </div>
          <div className="stat">
            <div className="k">events</div>
            <div className="v row">
              <span className={`dot ${connection === 'open' ? 'dot-ok' : 'dot-danger'}`} />
              {connLabel}
            </div>
          </div>
          <div className="stat">
            <div className="k">running</div>
            <div className="v">{status ? status.runs.running : '—'}</div>
          </div>
          <div className="stat">
            <div className="k">queued</div>
            <div className="v">{status ? status.runs.queued : '—'}</div>
          </div>
        </div>

        {error && (
          <div className="card ep bad">
            <div className="ep-head">
              <span className="dot dot-danger" />
              <h3>Core unreachable</h3>
            </div>
            <div className="ep-detail">{error}</div>
          </div>
        )}

        <div className="section-head">
          <h2>Endpoints by role</h2>
          <p>Probed live. No automatic fallback anywhere.</p>
        </div>
        {status ? (
          <div className="status-grid">
            {status.endpoints.map((ep) => (
              <EndpointCard key={`${ep.role}:${ep.base_url}:${ep.model}`} ep={ep} />
            ))}
          </div>
        ) : (
          !error && <div className="empty small">Probing…</div>
        )}

        <div className="section-head">
          <h2>Tool providers</h2>
          <div className="row">
            <p>Built-in plus every MCP server.</p>
            <button type="button" className="btn btn-secondary btn-sm" onClick={reloadTools} disabled={reloading}>
              <Icon name="refresh" size={14} />
              {reloading ? 'Reloading…' : 'Reload tools'}
            </button>
          </div>
        </div>
        {status && (
          <div className="status-grid">
            {(status.tools ?? []).map((p) => (
              <ProviderCard key={p.name} p={p} />
            ))}
            {(status.tools ?? []).length === 0 && <div className="empty small">No tool providers reported.</div>}
          </div>
        )}

        <ToolsList tools={tools} error={toolsError} />
      </div>
    </div>
  );
}

function EndpointCard({ ep }: { ep: EndpointStatus }) {
  const served = ep.models.includes(ep.model);
  const modelState = !ep.ok ? 'unreachable' : ep.model ? (served ? 'served' : 'missing') : 'no model set';
  return (
    <div className={`card ep ${ep.ok && (served || !ep.model) ? 'ok' : 'bad'}`} aria-label={`${ep.role} endpoint ${ep.ok ? 'ok' : 'failing'}`}>
      <div className="ep-head">
        <span className={`dot ${ep.ok ? 'dot-ok' : 'dot-danger'}`} />
        <h3>{ep.role}</h3>
        {ep.think && <span className="chip chip-outline">think</span>}
        <span className={`chip ${ep.ok ? 'chip-ok' : 'chip-danger'}`}>{ep.ok ? 'ok' : 'down'}</span>
      </div>
      <div className="ep-line">
        <span className="k">provider</span>
        <span className="v">{ep.provider}</span>
      </div>
      <div className="ep-line">
        <span className="k">endpoint</span>
        <span className="v mono" title={ep.base_url}>
          {ep.base_url}
        </span>
      </div>
      <div className="ep-line">
        <span className="k">model</span>
        <span className="v mono" title={ep.model}>
          {ep.model || '—'}
          {ep.model && (
            <>
              {' '}
              <span className={`chip ${modelState === 'served' ? 'chip-ok' : 'chip-danger'}`}>{modelState}</span>
            </>
          )}
        </span>
      </div>
      <div className="ep-line">
        <span className="k">latency</span>
        <span className="v">{ep.latency_ms !== null ? formatDuration(ep.latency_ms) : '—'}</span>
      </div>
      {ep.detail && <div className={ep.ok ? 'small muted' : 'ep-detail'}>{ep.detail}</div>}
      {ep.models.length > 0 && (
        <details className="ep-models">
          <summary>{ep.models.length} models served</summary>
          <ul>
            {ep.models.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function ProviderCard({ p }: { p: ToolProviderStatus }) {
  return (
    <div className={`card ep ${p.ok ? 'ok' : 'bad'}`} aria-label={`${p.name} tools ${p.ok ? 'ok' : 'failing'}`}>
      <div className="ep-head">
        <span className={`dot ${p.ok ? 'dot-ok' : 'dot-danger'}`} />
        <h3 className="mono" style={{ textTransform: 'none' }}>
          {p.name}
        </h3>
        <span className={`chip ${p.ok ? 'chip-ok' : 'chip-danger'}`}>{p.ok ? `${p.tools} tools` : 'down'}</span>
      </div>
      {p.error && <div className="ep-detail">{p.error}</div>}
    </div>
  );
}

function ToolsList({ tools, error }: { tools: ToolSpec[] | null; error: string | null }) {
  const groups = useMemo(() => {
    const m = new Map<string, ToolSpec[]>();
    for (const t of tools ?? []) {
      const list = m.get(t.provider) ?? [];
      list.push(t);
      m.set(t.provider, list);
    }
    return [...m.entries()].sort(([a], [b]) => (a === 'builtin' ? -1 : b === 'builtin' ? 1 : a.localeCompare(b)));
  }, [tools]);

  return (
    <details className="card tools-list">
      <summary>
        <Icon name="chevronRight" size={14} className="chev" />
        <span>Tools</span>
        <span className="muted small">{tools ? `${tools.length} exposed to the model` : error ? 'unavailable' : 'loading…'}</span>
      </summary>
      <div className="tools-body">
        {error && <div className="field-error">{error}</div>}
        {groups.map(([provider, list]) => (
          <div key={provider} className="tools-group">
            <div className="field-label">{provider}</div>
            {list.map((t) => (
              <div key={t.name} className="tool-row">
                <span className="mono tool-row-name">{t.name}</span>
                <span className="row" style={{ gap: 4 }}>
                  {t.read_only && <span className="chip chip-outline">read-only</span>}
                  {t.destructive && <span className="chip chip-warn">destructive</span>}
                  {t.idempotent && <span className="chip chip-outline">idempotent</span>}
                </span>
                <span className="small muted truncate tool-row-desc">{t.description.split('\n')[0]}</span>
              </div>
            ))}
          </div>
        ))}
        {tools?.length === 0 && <div className="empty small">No tools are exposed right now.</div>}
      </div>
    </details>
  );
}
