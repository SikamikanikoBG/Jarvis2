import { Icon } from '../components/Icon';
import { IconButton, Switch } from '../components/primitives';
import type { TriageSettings } from '../protocol/types';

interface Props {
  value: TriageSettings;
  hosts: string[];
  onChange: (v: TriageSettings) => void;
  error?: string;
}

const lines = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean);
const csv = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);

/** The `triage` settings block: host, accounts, demand routing, categories. */
export function TriageSection({ value, hosts, onChange, error }: Props) {
  const patch = (p: Partial<TriageSettings>) => onChange({ ...value, ...p });
  const cats = value.categories;
  const setCat = (i: number, field: 'name' | 'folder' | 'rule', v: string) => patch({ categories: cats.map((c, j) => (j === i ? { ...c, [field]: v } : c)) });

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Triage</h2>
        <p>Background mail triage through the host that owns Outlook.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Enable triage" />
        <span className="small">Enabled</span>
        <span className="field-hint">Runs every {value.interval_min} min as a system schedule.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label>Interval (minutes)</label>
          <input className="input" type="number" min={1} step="any" value={value.interval_min} onChange={(e) => patch({ interval_min: Math.max(1, Number(e.target.value) || 1) })} />
        </div>
        <div className="field">
          <label>Host (MCP server)</label>
          <select className="select" value={value.host} onChange={(e) => patch({ host: e.target.value })}>
            <option value="">— none —</option>
            {hosts.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
            {value.host && !hosts.includes(value.host) && <option value={value.host}>{value.host} (not configured)</option>}
          </select>
        </div>
        <div className="field">
          <label>Demand root folder</label>
          <input className="input" value={value.demand_root} onChange={(e) => patch({ demand_root: e.target.value })} placeholder="Demands" />
        </div>
        <div className="field">
          <label>Demand prefixes (comma-separated)</label>
          <input className="input mono" value={value.demand_prefixes.join(', ')} onChange={(e) => patch({ demand_prefixes: csv(e.target.value) })} placeholder="DM-" />
        </div>
        <div className="field span-2">
          <label>Accounts (one per line)</label>
          <textarea className="textarea mono" rows={2} value={value.accounts.join('\n')} onChange={(e) => patch({ accounts: lines(e.target.value) })} placeholder="aapostolov@postbank.bg" />
        </div>
      </div>
      <div className="field">
        <label>Categories</label>
        <div className="budget-wrap">
          <table className="budget-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Folder</th>
                <th>Rule</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {cats.map((c, i) => (
                <tr key={i}>
                  <td>
                    <input className="input" value={c.name ?? ''} onChange={(e) => setCat(i, 'name', e.target.value)} aria-label={`Category ${i + 1} name`} />
                  </td>
                  <td>
                    <input className="input" value={c.folder ?? ''} onChange={(e) => setCat(i, 'folder', e.target.value)} aria-label={`Category ${i + 1} folder`} />
                  </td>
                  <td>
                    <input className="input" value={c.rule ?? ''} onChange={(e) => setCat(i, 'rule', e.target.value)} placeholder="what belongs here" aria-label={`Category ${i + 1} rule`} />
                  </td>
                  <td>
                    <IconButton icon="trash" label="Remove category" size="sm" onClick={() => patch({ categories: cats.filter((_, j) => j !== i) })} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" style={{ alignSelf: 'flex-start' }} onClick={() => patch({ categories: [...cats, { name: '', folder: '', rule: '' }] })}>
          <Icon name="plus" size={14} />
          Add category
        </button>
      </div>
    </section>
  );
}
