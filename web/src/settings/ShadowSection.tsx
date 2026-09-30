import { Switch } from '../components/primitives';
import type { ShadowSettings } from '../protocol/types';

interface Props {
  value: ShadowSettings;
  onChange: (v: ShadowSettings) => void;
  error?: string;
}

const POINTS: { id: string; label: string }[] = [
  { id: 'mail', label: 'mail — triage category and alerts' },
  { id: 'rsvp', label: 'rsvp — meeting answers' },
  { id: 'preflight', label: 'preflight — tier and skills' },
  { id: 'guardrail', label: 'guardrail — text about to go out' },
];

const csv = (s: string) =>
  s
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean);

/**
 * Laya's second opinion (features/shadow.py): after a real decision the same input goes to the
 * small typed-decision model and both answers are written down side by side. Nothing Jarvis does
 * depends on it, so every field here changes only what is measured, never what happens.
 */
export function ShadowSection({ value, onChange, error }: Props) {
  const patch = (p: Partial<ShadowSettings>) => onChange({ ...value, ...p });
  const togglePoint = (id: string, on: boolean) =>
    patch({ points: on ? [...value.points.filter((p) => p !== id), id] : value.points.filter((p) => p !== id) });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Second opinion (Laya)</h2>
        <p>
          After a real decision the same input goes to Laya and both answers are recorded side by side. It is never obeyed: Laya down,
          slow or wrong changes nothing but the record.
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Record a second opinion" />
        <span className="small">Enabled</span>
        <span className="field-hint">Off: nothing is recorded.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="shadow-url">Laya URL</label>
          <input id="shadow-url" className="input" value={value.laya_url} onChange={(e) => patch({ laya_url: e.target.value })} placeholder="http://…:9140" disabled={!value.enabled} />
          <div className="field-hint">Empty: record the input and Jarvis&apos;s answer only, to replay a model over later.</div>
        </div>
        <div className="field">
          <label htmlFor="shadow-timeout">Timeout (seconds)</label>
          <input
            id="shadow-timeout"
            className="input"
            type="number"
            min={1}
            step="any"
            value={value.timeout_s}
            onChange={(e) => patch({ timeout_s: Math.max(1, Number(e.target.value) || 1) })}
            disabled={!value.enabled}
          />
        </div>
        <div className="field">
          <label htmlFor="shadow-pending">Calls in flight</label>
          <input
            id="shadow-pending"
            className="input"
            type="number"
            min={1}
            step="any"
            value={value.max_pending}
            onChange={(e) => patch({ max_pending: Math.max(1, Math.round(Number(e.target.value)) || 1) })}
            disabled={!value.enabled}
          />
          <div className="field-hint">Past this a new observation is dropped, so a burst of mail never waits behind Laya.</div>
        </div>
        <div className="field">
          <label htmlFor="shadow-guard">Guardrail tools</label>
          <input
            id="shadow-guard"
            className="input mono"
            value={value.guardrail_tools.join(', ')}
            onChange={(e) => patch({ guardrail_tools: csv(e.target.value) })}
            placeholder="*.outlook_send, notify.discord"
            disabled={!value.enabled}
          />
          <div className="field-hint">Comma-separated; exact names or *.suffix. Their outgoing text is what the guardrail point sees.</div>
        </div>
        <div className="field span-2">
          <label>Decision points observed</label>
          {POINTS.map((p) => (
            <div key={p.id} className="think-row">
              <Switch checked={value.points.includes(p.id)} onChange={(v) => togglePoint(p.id, v)} label={p.label} />
              <span className="small">{p.label}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
