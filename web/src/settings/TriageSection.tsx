import { Icon } from '../components/Icon';
import { IconButton, Switch } from '../components/primitives';
import type { TriageRules, TriageSettings } from '../protocol/types';

interface Props {
  value: TriageSettings;
  hosts: string[];
  onChange: (v: TriageSettings) => void;
  error?: string;
}

const lines = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean);
const csv = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);

const emptyRules = (): TriageRules => ({ categories: [], instructions: '', fallback_category: '', demand_routing: true });

/** The `triage` settings block: host, accounts, demand routing, and one rule set per mailbox. */
export function TriageSection({ value, hosts, onChange, error }: Props) {
  const patch = (p: Partial<TriageSettings>) => onChange({ ...value, ...p });
  const defaults: TriageRules = {
    categories: value.categories,
    instructions: value.instructions,
    fallback_category: value.fallback_category,
    demand_routing: true,
  };
  const overrides = value.account_rules ?? {};
  const withoutOverride = value.accounts.filter((a) => !(a in overrides));

  const patchDefaults = (r: TriageRules) => patch({ categories: r.categories, instructions: r.instructions, fallback_category: r.fallback_category });
  const patchOverride = (account: string, r: TriageRules) => patch({ account_rules: { ...overrides, [account]: r } });
  const dropOverride = (account: string) => patch({ account_rules: Object.fromEntries(Object.entries(overrides).filter(([a]) => a !== account)) });

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Triage</h2>
        <p>Background mail triage through the host that owns Outlook. Dry-run it before switching it on: it moves real mail.</p>
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
          <span className="field-hint">Empty = every account Outlook reports. Each one is sorted by the default rules below unless it has its own.</span>
        </div>
      </div>

      <RulesEditor title="Default rules" rules={defaults} onChange={patchDefaults} />

      {Object.entries(overrides).map(([account, rules]) => (
        <RulesEditor key={account} title={`Rules for ${account}`} rules={rules} onChange={(r) => patchOverride(account, r)} onRemove={() => dropOverride(account)} showDemandToggle />
      ))}

      {withoutOverride.length > 0 && (
        <div className="field">
          <label>Own rules for an account</label>
          <select
            className="select"
            value=""
            onChange={(e) => {
              if (e.target.value) patchOverride(e.target.value, emptyRules());
            }}
          >
            <option value="">— pick an account —</option>
            {withoutOverride.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <span className="field-hint">A personal mailbox wants different folders and different rules than the work one.</span>
        </div>
      )}
    </section>
  );
}

interface RulesProps {
  title: string;
  rules: TriageRules;
  onChange: (r: TriageRules) => void;
  onRemove?: () => void;
  showDemandToggle?: boolean;
}

/** Categories + instructions + catch-all: the same editor for the defaults and for one account. */
function RulesEditor({ title, rules, onChange, onRemove, showDemandToggle }: RulesProps) {
  const cats = rules.categories;
  const patch = (p: Partial<TriageRules>) => onChange({ ...rules, ...p });
  const setCat = (i: number, field: 'name' | 'folder' | 'rule', v: string) => patch({ categories: cats.map((c, j) => (j === i ? { ...c, [field]: v } : c)) });

  return (
    <div className="rules-editor">
      <div className="rules-head">
        <h3>{title}</h3>
        {onRemove && (
          <button type="button" className="btn btn-sm btn-secondary" onClick={onRemove}>
            Use the default rules
          </button>
        )}
      </div>
      <div className="field">
        <label>Instructions for the classifier</label>
        <textarea
          className="textarea"
          rows={5}
          value={rules.instructions}
          onChange={(e) => patch({ instructions: e.target.value })}
          placeholder="Who the owner is, what Cc-only means, the VIP list, hard exclusions, priority order between categories."
        />
        <span className="field-hint">Read before the category list on every mail. Plain text; Bulgarian or English.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label>When nothing fits</label>
          <select className="select" value={rules.fallback_category} onChange={(e) => patch({ fallback_category: e.target.value })}>
            <option value="">leave in Inbox</option>
            {cats
              .filter((c) => c.name)
              .map((c) => (
                <option key={c.name} value={c.name}>
                  move to “{c.name}”
                </option>
              ))}
          </select>
          <span className="field-hint">Pick the catch-all for inbox zero.</span>
        </div>
        {showDemandToggle && (
          <div className="field">
            <label>Demand routing</label>
            <div className="think-row">
              <Switch checked={rules.demand_routing} onChange={(v) => patch({ demand_routing: v })} label="Route DM-1234 mail into demand folders" />
              <span className="small">DM-1234 → Demands/DM-1234</span>
            </div>
          </div>
        )}
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
    </div>
  );
}
