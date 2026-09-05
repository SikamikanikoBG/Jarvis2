import { Switch } from '../components/primitives';
import type { MeetingRsvpSettings } from '../protocol/types';

interface Props {
  value: MeetingRsvpSettings;
  hosts: string[];
  onChange: (v: MeetingRsvpSettings) => void;
  error?: string;
}

const lines = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean);
const csv = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);
const int = (v: string, lo: number, hi: number, fallback: number) => {
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
};

/** The `rsvp` settings block: who gets an automatic answer to a meeting invite, and how. */
export function RsvpSection({ value, hosts, onChange, error }: Props) {
  const patch = (p: Partial<MeetingRsvpSettings>) => onChange({ ...value, ...p });
  const nobody = value.allowed_domains.length === 0 && value.vip.length === 0;

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Meeting auto-RSVP</h2>
        <p>Free slot → accept. Clash with a committed meeting → decline with free alternatives. A VIP is never declined. Invites from outside the allowed domains are left for you.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Enable auto-RSVP" />
        <span className="small">Enabled</span>
        <span className="field-hint">
          Every {value.interval_min} min. Sends real responses to organizers — dry-run first: <code>POST /api/rsvp/run?dry_run=true</code>.
        </span>
      </div>
      {value.enabled && nobody && <div className="field-error">No allowed domains and no VIPs: every invite will be left for you.</div>}
      <div className="form-grid">
        <div className="field">
          <label>Interval (minutes)</label>
          <input className="input" type="number" min={1} value={value.interval_min} onChange={(e) => patch({ interval_min: int(e.target.value, 1, 1440, 3) })} />
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
          <label>Account</label>
          <input className="input mono" value={value.account} onChange={(e) => patch({ account: e.target.value })} placeholder="default account" />
        </div>
        <div className="field">
          <label>Look ahead (days)</label>
          <input className="input" type="number" min={1} max={60} value={value.lookahead_days} onChange={(e) => patch({ lookahead_days: int(e.target.value, 1, 60, 5) })} />
        </div>
        <div className="field">
          <label>Allowed organizer domains (comma-separated)</label>
          <input className="input mono" value={value.allowed_domains.join(', ')} onChange={(e) => patch({ allowed_domains: csv(e.target.value) })} placeholder="postbank.bg" />
          <span className="field-hint">Empty answers nobody (fail closed).</span>
        </div>
        <div className="field">
          <label>Alternatives to propose on a decline</label>
          <input className="input" type="number" min={0} max={6} value={value.propose_slots} onChange={(e) => patch({ propose_slots: int(e.target.value, 0, 6, 3) })} />
        </div>
        <div className="field span-2">
          <label>VIPs — never declined (one address or domain per line)</label>
          <textarea className="textarea mono" rows={4} value={value.vip.join('\n')} onChange={(e) => patch({ vip: lines(e.target.value) })} placeholder="ceo@postbank.bg" />
        </div>
        <div className="field">
          <label>Work day starts (hour)</label>
          <input className="input" type="number" min={0} max={23} value={value.work_start_hour} onChange={(e) => patch({ work_start_hour: int(e.target.value, 0, 23, 9) })} />
        </div>
        <div className="field">
          <label>Work day ends (hour)</label>
          <input className="input" type="number" min={1} max={24} value={value.work_end_hour} onChange={(e) => patch({ work_end_hour: int(e.target.value, 1, 24, 18) })} />
        </div>
      </div>
      <div className="think-row">
        <Switch checked={value.remove_canceled} onChange={(v) => patch({ remove_canceled: v })} label="Remove cancelled meetings" />
        <span className="small">Remove cancelled meetings from the calendar</span>
        <span className="field-hint">Meetings the organizer called off are deleted locally; nothing is sent.</span>
      </div>
    </section>
  );
}
