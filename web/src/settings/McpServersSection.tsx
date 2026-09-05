import { useState } from 'react';
import { Icon } from '../components/Icon';
import { IconButton, InlineConfirm, Switch } from '../components/primitives';
import type { McpServerSpec, McpTransport } from '../protocol/types';

interface Props {
  servers: McpServerSpec[];
  onChange: (servers: McpServerSpec[]) => void;
  error?: string;
}

const NAME_RE = /^[a-z][a-z0-9_-]{0,31}$/;

function blank(): McpServerSpec {
  return { name: '', transport: 'stdio', command: '', args: [], env: {}, url: null, headers: {}, enabled: true, timeout_s: 60 };
}

/** External MCP servers: list with enable toggles, inline add/edit form, inline delete confirm. */
export function McpServersSection({ servers, onChange, error }: Props) {
  const [editing, setEditing] = useState<number | 'new' | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);

  const update = (i: number, patch: Partial<McpServerSpec>) => onChange(servers.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const remove = (i: number) => {
    onChange(servers.filter((_, j) => j !== i));
    setConfirmDelete(null);
  };
  const save = (spec: McpServerSpec) => {
    if (editing === 'new') onChange([...servers, spec]);
    else if (typeof editing === 'number') onChange(servers.map((s, j) => (j === editing ? spec : s)));
    setEditing(null);
  };

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>MCP servers</h2>
        <p>Their tools appear as {'<name>.<tool>'}. Saved as one list.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="mcp-list">
        {servers.length === 0 && <div className="empty small">No external servers. Built-in tools are always available.</div>}
        {servers.map((s, i) =>
          editing === i ? (
            <McpForm key={s.name || i} initial={s} existing={servers.filter((_, j) => j !== i).map((x) => x.name)} onSave={save} onCancel={() => setEditing(null)} />
          ) : confirmDelete === i ? (
            <div key={s.name || i} className="mcp-row">
              <InlineConfirm text={`Remove "${s.name}"?`} confirmLabel="Remove" danger onConfirm={() => remove(i)} onCancel={() => setConfirmDelete(null)} />
            </div>
          ) : (
            <div key={s.name || i} className={`mcp-row${s.enabled ? '' : ' disabled'}`}>
              <Switch checked={s.enabled} onChange={(v) => update(i, { enabled: v })} label={`Enable ${s.name}`} />
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="row">
                  <span className="mono" style={{ fontWeight: 600 }}>
                    {s.name}
                  </span>
                  <span className="chip chip-outline">{s.transport === 'stdio' ? 'stdio' : 'http'}</span>
                </div>
                <div className="small muted truncate">{s.transport === 'stdio' ? [s.command, ...s.args].filter(Boolean).join(' ') : s.url}</div>
              </div>
              <IconButton icon="edit" label={`Edit ${s.name}`} size="sm" onClick={() => setEditing(i)} />
              <IconButton icon="trash" label={`Remove ${s.name}`} size="sm" onClick={() => setConfirmDelete(i)} />
            </div>
          ),
        )}
        {editing === 'new' ? (
          <McpForm initial={blank()} existing={servers.map((x) => x.name)} onSave={save} onCancel={() => setEditing(null)} />
        ) : (
          <button type="button" className="btn btn-secondary btn-sm" style={{ alignSelf: 'flex-start' }} onClick={() => setEditing('new')}>
            <Icon name="plus" size={15} />
            Add server
          </button>
        )}
      </div>
    </section>
  );
}

const linesToList = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean);
const linesToMap = (s: string): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const line of linesToList(s)) {
    const eq = line.indexOf('=');
    const key = (eq > 0 ? line.slice(0, eq) : line).trim();
    if (key) out[key] = eq > 0 ? line.slice(eq + 1).trim() : '';
  }
  return out;
};
const mapToLines = (m: Record<string, string>) =>
  Object.entries(m)
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');

function McpForm({ initial, existing, onSave, onCancel }: { initial: McpServerSpec; existing: string[]; onSave: (s: McpServerSpec) => void; onCancel: () => void }) {
  const [name, setName] = useState(initial.name);
  const [transport, setTransport] = useState<McpTransport>(initial.transport);
  const [command, setCommand] = useState(initial.command ?? '');
  const [args, setArgs] = useState(initial.args.join('\n'));
  const [env, setEnv] = useState(mapToLines(initial.env));
  const [url, setUrl] = useState(initial.url ?? '');
  const [headers, setHeaders] = useState(mapToLines(initial.headers));
  const [timeout, setTimeout_] = useState(initial.timeout_s);
  const [enabled, setEnabled] = useState(initial.enabled);

  const nameError = !NAME_RE.test(name) ? 'lowercase letters, digits, _ or -, up to 32 chars, starting with a letter' : name === 'jarvis' ? '"jarvis" is reserved for built-in tools' : existing.includes(name) ? 'name already used' : null;
  const transportError = transport === 'stdio' && !command.trim() ? 'stdio needs a command' : transport === 'streamable_http' && !url.trim() ? 'streamable_http needs a url' : null;
  const valid = !nameError && !transportError;

  const submit = () => {
    if (!valid) return;
    onSave({
      name,
      transport,
      command: transport === 'stdio' ? command.trim() : null,
      args: transport === 'stdio' ? linesToList(args) : [],
      env: transport === 'stdio' ? linesToMap(env) : {},
      url: transport === 'streamable_http' ? url.trim() : null,
      headers: transport === 'streamable_http' ? linesToMap(headers) : {},
      enabled,
      timeout_s: timeout,
    });
  };

  return (
    <div className="mcp-form" role="group" aria-label="MCP server">
      <div className="form-grid">
        <div className="field">
          <label>Name</label>
          <input className={`input${nameError && name ? ' invalid' : ''}`} value={name} onChange={(e) => setName(e.target.value)} placeholder="fetch" autoFocus />
          {nameError && name ? <div className="field-error">{nameError}</div> : <div className="field-hint">Tools appear as {name || 'name'}.{'<tool>'}</div>}
        </div>
        <div className="field">
          <label>Transport</label>
          <select className="select" value={transport} onChange={(e) => setTransport(e.target.value as McpTransport)}>
            <option value="stdio">stdio</option>
            <option value="streamable_http">streamable_http</option>
          </select>
        </div>
        <div className="field">
          <label>Timeout (s)</label>
          <input className="input" type="number" min={1} value={timeout} onChange={(e) => setTimeout_(Math.max(1, Number(e.target.value) || 1))} />
        </div>
        <div className="field">
          <label>Enabled</label>
          <div className="row" style={{ minHeight: 38 }}>
            <Switch checked={enabled} onChange={setEnabled} label="Enabled" />
          </div>
        </div>
        {transport === 'stdio' ? (
          <>
            <div className="field span-2">
              <label>Command</label>
              <input className="input mono" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="{python}" />
              <div className="field-hint">{'{python}'} = the core's own interpreter.</div>
            </div>
            <div className="field">
              <label>Arguments (one per line)</label>
              <textarea className="textarea mono" rows={3} value={args} onChange={(e) => setArgs(e.target.value)} placeholder={'-m\nmcp_server_fetch'} />
            </div>
            <div className="field">
              <label>Environment (KEY=value per line)</label>
              <textarea className="textarea mono" rows={3} value={env} onChange={(e) => setEnv(e.target.value)} />
            </div>
          </>
        ) : (
          <>
            <div className="field span-2">
              <label>URL</label>
              <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://host:port/mcp" />
            </div>
            <div className="field span-2">
              <label>Headers (Name=value per line)</label>
              <textarea className="textarea mono" rows={3} value={headers} onChange={(e) => setHeaders(e.target.value)} placeholder="Authorization=Bearer …" />
            </div>
          </>
        )}
      </div>
      {transportError && <div className="field-error">{transportError}</div>}
      <div className="row">
        <button type="button" className="btn btn-primary btn-sm" onClick={submit} disabled={!valid}>
          {initial.name ? 'Apply' : 'Add'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onCancel}>
          Cancel
        </button>
        <span className="field-hint">Applies to the list; use Save below to persist.</span>
      </div>
    </div>
  );
}
