import { Switch } from '../components/primitives';
import { ADDRESS_STYLE, FORMALITY, HUMOR, VERBOSITY, type Personality } from '../protocol/types';

interface Props {
  value: Personality;
  onChange: (v: Personality) => void;
  error?: string;
}

const ADDRESS_LABEL: Record<string, string> = { name: 'by name', sir: '“sir”', neutral: 'neutral' };

/** Tone dials. Deliberately not a "be nicer" switch: they change how, never whether. */
export function PersonalitySection({ value, onChange, error }: Props) {
  const patch = (p: Partial<Personality>) => onChange({ ...value, ...p });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Personality — how Jarvis speaks</h2>
        <p>Tone controls how Jarvis says things, never whether — disagreement always ships.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Apply the personality" />
        <span className="small">Enabled</span>
        <span className="field-hint">Off = plain, neutral phrasing with no persona in the prompt.</span>
      </div>
      <div className="tone-grid">
        <Segmented label="Formality" options={FORMALITY} value={value.formality} onChange={(v) => patch({ formality: v })} disabled={!value.enabled} />
        <Segmented label="Humour" options={HUMOR} value={value.humor} onChange={(v) => patch({ humor: v })} disabled={!value.enabled} />
        <Segmented label="Verbosity" options={VERBOSITY} value={value.verbosity} onChange={(v) => patch({ verbosity: v })} disabled={!value.enabled} />
        <Segmented label="Address" options={ADDRESS_STYLE} value={value.address_style} onChange={(v) => patch({ address_style: v })} disabled={!value.enabled} labelFor={(o) => ADDRESS_LABEL[o] ?? o} />
      </div>
      <div className="field">
        <label htmlFor="persona">Persona</label>
        <textarea id="persona" className="textarea" rows={3} value={value.persona} onChange={(e) => patch({ persona: e.target.value })} disabled={!value.enabled} placeholder="Dry, quick and unimpressed by hype…" />
        <div className="field-hint">Free text, added to the system prompt. Describe the voice, not the rules.</div>
      </div>
    </section>
  );
}

function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
  disabled,
  labelFor,
}: {
  label: string;
  options: readonly T[];
  value: T;
  onChange: (v: T) => void;
  disabled?: boolean;
  labelFor?: (o: T) => string;
}) {
  return (
    <div className="field">
      <span className="field-label" id={`seg-${label}`}>
        {label}
      </span>
      <div className={`seg${disabled ? ' disabled' : ''}`} role="radiogroup" aria-labelledby={`seg-${label}`}>
        {options.map((o) => (
          <button key={o} type="button" role="radio" aria-checked={value === o} className={value === o ? 'active' : ''} disabled={disabled} onClick={() => onChange(o)}>
            {labelFor ? labelFor(o) : o}
          </button>
        ))}
      </div>
    </div>
  );
}
