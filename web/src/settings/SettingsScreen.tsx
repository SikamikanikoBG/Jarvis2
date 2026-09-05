import { useEffect, useMemo, useState } from 'react';
import { ApiError, api, describeError, fieldErrors } from '../api/client';
import { Switch } from '../components/primitives';
import type { ThemePref } from '../lib/theme';
import { ROLE_NAMES, RUN_KINDS, THINK_LEVELS, type ModelSpec, type Provider, type RoleName, type RunKind, type Settings, type ThinkLevel } from '../protocol/types';
import { useStore } from '../store/store';
import { McpServersSection } from './McpServersSection';

const PROVIDERS: Provider[] = ['ollama', 'vllm'];
const THINK_HINT = 'Only chat may think. Planners, classifiers and judges with thinking on spend their whole budget thinking.';

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

type Errors = Record<string, string>;

export function SettingsScreen() {
  const [original, setOriginal] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const themePref = useStore((s) => s.themePref);
  const setTheme = useStore((s) => s.setTheme);

  useEffect(() => {
    api.settings
      .get()
      .then((s) => {
        setOriginal(s);
        setDraft(s);
      })
      .catch((e: unknown) => setLoadError(e instanceof ApiError ? describeError(e.status, e.body) : String(e)));
  }, []);

  const dirtyKeys = useMemo(() => {
    if (!original || !draft) return [];
    return (Object.keys(draft) as (keyof Settings)[]).filter((k) => !deepEqual(draft[k], original[k]));
  }, [original, draft]);

  const patch = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
    setSavedAt(null);
  };
  const patchRole = (role: RoleName, spec: Partial<ModelSpec>) => {
    if (!draft) return;
    patch('roles', { ...draft.roles, [role]: { ...draft.roles[role], ...spec } });
  };
  const patchBudget = (kind: RunKind, field: 'max_steps' | 'max_tokens' | 'max_seconds', value: number) => {
    if (!draft) return;
    patch('budgets', { ...draft.budgets, [kind]: { ...draft.budgets[kind], [field]: value } });
  };

  const save = async () => {
    if (!draft || dirtyKeys.length === 0) return;
    setSaving(true);
    setErrors({});
    const body: Partial<Settings> = {};
    for (const k of dirtyKeys) (body as Record<string, unknown>)[k] = draft[k];
    try {
      const saved = await api.settings.patch(body);
      setOriginal(saved);
      setDraft(saved);
      setSavedAt(Date.now());
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const fe = fieldErrors(e.body);
        setErrors(Object.keys(fe).length ? fe : { _: describeError(e.status, e.body) });
      } else {
        setErrors({ _: e instanceof ApiError ? describeError(e.status, e.body) : String(e) });
      }
    } finally {
      setSaving(false);
    }
  };

  if (loadError) {
    return (
      <div className="screen">
        <div className="screen-inner">
          <h1>Settings</h1>
          <div className="empty">
            <strong>Could not load settings</strong>
            <span>{loadError}</span>
          </div>
        </div>
      </div>
    );
  }
  if (!draft) {
    return (
      <div className="screen">
        <div className="screen-inner">
          <h1>Settings</h1>
          <div className="empty">Loading…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="screen">
      <form
        className="screen-inner"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <h1>Settings</h1>

        <section className="card role-card">
          <div className="section-head">
            <h2>Appearance</h2>
            <p>Stored in this browser only.</p>
          </div>
          <div className="seg" role="radiogroup" aria-label="Theme">
            {(['system', 'light', 'dark'] as ThemePref[]).map((p) => (
              <button key={p} type="button" role="radio" aria-checked={themePref === p} className={themePref === p ? 'active' : ''} onClick={() => setTheme(p)}>
                {p[0]?.toUpperCase() + p.slice(1)}
              </button>
            ))}
          </div>
        </section>

        <section className="card role-card">
          <div className="section-head">
            <h2>General</h2>
          </div>
          <div className="form-grid">
            <Field label="Assistant name" error={errors.assistant_name}>
              <input className="input" value={draft.assistant_name} onChange={(e) => patch('assistant_name', e.target.value)} />
            </Field>
            <Field label="Your name" error={errors.user_name}>
              <input className="input" value={draft.user_name} onChange={(e) => patch('user_name', e.target.value)} />
            </Field>
            <Field label="Timezone" error={errors.timezone}>
              <input className="input" value={draft.timezone} onChange={(e) => patch('timezone', e.target.value)} placeholder="Europe/Sofia" />
            </Field>
            <Field label="STT URL" error={errors.stt_url} hint="Whisper endpoint; empty disables voice input.">
              <input className="input" value={draft.stt_url ?? ''} onChange={(e) => patch('stt_url', e.target.value || null)} placeholder="http://…" />
            </Field>
            <Field label="STT languages" error={errors.stt_languages} hint="Comma-separated, e.g. bg, en">
              <input
                className="input"
                value={draft.stt_languages.join(', ')}
                onChange={(e) =>
                  patch(
                    'stt_languages',
                    e.target.value
                      .split(',')
                      .map((x) => x.trim())
                      .filter(Boolean),
                  )
                }
              />
            </Field>
            <Field label="Concurrent runs per endpoint" error={errors.max_concurrent_runs_per_endpoint}>
              <NumberInput value={draft.max_concurrent_runs_per_endpoint} min={1} onChange={(v) => patch('max_concurrent_runs_per_endpoint', v ?? 1)} />
            </Field>
            <Field label="Repeated call threshold" error={errors.repeated_call_threshold} hint="Identical tool calls before the judge is summoned.">
              <NumberInput value={draft.repeated_call_threshold} min={1} onChange={(v) => patch('repeated_call_threshold', v ?? 1)} />
            </Field>
            <Field label="Language hint" error={errors.language_hint} className="span-2">
              <textarea className="textarea" value={draft.language_hint} onChange={(e) => patch('language_hint', e.target.value)} rows={2} />
            </Field>
          </div>
        </section>

        <section style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="section-head">
            <h2>Model roles</h2>
            <p>Which endpoint serves each role. No fallbacks: a missing model stays missing.</p>
          </div>
          {errors.roles && <div className="field-error">{errors.roles}</div>}
          {ROLE_NAMES.map((role) => (
            <RoleCard key={role} role={role} spec={draft.roles[role]} onChange={(s) => patchRole(role, s)} />
          ))}
        </section>

        <McpServersSection servers={draft.mcp_servers} onChange={(list) => patch('mcp_servers', list)} error={errors.mcp_servers} />

        <section className="card role-card">
          <div className="section-head">
            <h2>Budgets per run kind</h2>
            <p>Exhaustion ends the run with a summary, never silently.</p>
          </div>
          {errors.budgets && <div className="field-error">{errors.budgets}</div>}
          <div className="budget-wrap">
            <table className="budget-table">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Max steps</th>
                  <th>Max tokens</th>
                  <th>Max seconds</th>
                </tr>
              </thead>
              <tbody>
                {RUN_KINDS.map((kind) => {
                  const b = draft.budgets[kind];
                  if (!b) return null;
                  return (
                    <tr key={kind}>
                      <td>{kind}</td>
                      <td>
                        <NumberInput value={b.max_steps} min={1} onChange={(v) => patchBudget(kind, 'max_steps', v ?? 1)} label={`${kind} max steps`} />
                      </td>
                      <td>
                        <NumberInput value={b.max_tokens} min={1} onChange={(v) => patchBudget(kind, 'max_tokens', v ?? 1)} label={`${kind} max tokens`} />
                      </td>
                      <td>
                        <NumberInput value={b.max_seconds} min={1} onChange={(v) => patchBudget(kind, 'max_seconds', v ?? 1)} label={`${kind} max seconds`} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <div className="savebar">
          <button type="submit" className="btn btn-primary" disabled={saving || dirtyKeys.length === 0}>
            {saving ? 'Saving…' : dirtyKeys.length ? `Save ${dirtyKeys.length} change${dirtyKeys.length > 1 ? 's' : ''}` : 'No changes'}
          </button>
          {dirtyKeys.length > 0 && (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                setDraft(original);
                setErrors({});
              }}
            >
              Discard
            </button>
          )}
          {errors._ && <span className="msg-err">{errors._}</span>}
          {savedAt && dirtyKeys.length === 0 && <span className="msg-ok">Saved.</span>}
        </div>
      </form>
    </div>
  );
}

function Field({ label, hint, error, className, children }: { label: string; hint?: string; error?: string; className?: string; children: React.ReactNode }) {
  return (
    <div className={`field${className ? ` ${className}` : ''}`}>
      <label>{label}</label>
      {children}
      {error ? <div className="field-error">{error}</div> : hint ? <div className="field-hint">{hint}</div> : null}
    </div>
  );
}

/** `step="any"` on purpose: a fixed step makes the browser block submit for values like num_ctx=8192. */
function NumberInput({
  value,
  onChange,
  min,
  max,
  placeholder,
  nullable,
  label,
  disabled,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  min?: number;
  max?: number;
  placeholder?: string;
  nullable?: boolean;
  label?: string;
  disabled?: boolean;
}) {
  return (
    <input
      className="input"
      type="number"
      inputMode="decimal"
      value={value ?? ''}
      min={min}
      max={max}
      step="any"
      placeholder={placeholder}
      aria-label={label}
      disabled={disabled}
      onChange={(e) => {
        if (e.target.value === '') {
          if (nullable) onChange(null);
          return;
        }
        const n = Number(e.target.value);
        if (Number.isFinite(n)) onChange(n);
      }}
    />
  );
}

function RoleCard({ role, spec, onChange }: { role: RoleName; spec: ModelSpec; onChange: (s: Partial<ModelSpec>) => void }) {
  const mayThink = role === 'chat';
  const ollama = spec.provider === 'ollama';
  return (
    <div className="card role-card">
      <div className="role-head">
        <h3>{role}</h3>
        <span className="field-hint">{ROLE_HINTS[role]}</span>
      </div>
      <div className="form-grid">
        <Field label="Provider">
          <select className="select" value={spec.provider} onChange={(e) => onChange({ provider: e.target.value as Provider })}>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
            {spec.provider === 'fake' && <option value="fake">fake</option>}
          </select>
        </Field>
        <Field label="Base URL">
          <input className="input" value={spec.base_url} onChange={(e) => onChange({ base_url: e.target.value })} placeholder="http://host:11434" />
        </Field>
        <Field label="Model">
          <input className="input" value={spec.model} onChange={(e) => onChange({ model: e.target.value })} placeholder="qwen3.8-27b" />
        </Field>
        <Field label="Context (num_ctx)" hint="Allocates VRAM. Empty = model default.">
          <NumberInput value={spec.num_ctx} min={512} nullable onChange={(v) => onChange({ num_ctx: v })} placeholder="default" />
        </Field>
        <Field label="Temperature">
          <NumberInput value={spec.temperature} min={0} max={2} onChange={(v) => onChange({ temperature: v ?? 0 })} />
        </Field>
        <Field label="Max tokens" hint="Per reply. Empty = unlimited.">
          <NumberInput value={spec.max_tokens} min={1} nullable onChange={(v) => onChange({ max_tokens: v })} placeholder="unlimited" />
        </Field>
        <Field label="Timeout (s)">
          <NumberInput value={spec.timeout_s} min={1} onChange={(v) => onChange({ timeout_s: v ?? 1 })} />
        </Field>
        <Field label="Keep alive" hint={ollama ? 'Ollama only, e.g. 30m' : 'Ollama only'}>
          <input className="input" value={spec.keep_alive ?? ''} disabled={!ollama} onChange={(e) => onChange({ keep_alive: e.target.value || null })} placeholder="30m" />
        </Field>
      </div>
      <div className="think-row">
        <Switch checked={spec.think} disabled={!mayThink} onChange={(v) => onChange({ think: v, think_level: v ? spec.think_level : null })} label={`Thinking for ${role}`} />
        <span className="small">Thinking</span>
        {mayThink && spec.think && (
          <div className="seg" role="radiogroup" aria-label="Thinking level">
            {([null, ...THINK_LEVELS] as (ThinkLevel | null)[]).map((lvl) => (
              <button key={lvl ?? 'default'} type="button" role="radio" aria-checked={spec.think_level === lvl} className={spec.think_level === lvl ? 'active' : ''} onClick={() => onChange({ think_level: lvl })}>
                {lvl ?? 'model default'}
              </button>
            ))}
          </div>
        )}
        <span className="field-hint">{mayThink ? 'Reasoning is streamed into the fold and stored beside the reply. Level: Ollama think level / vLLM reasoning_effort.' : THINK_HINT}</span>
      </div>
    </div>
  );
}

const ROLE_HINTS: Record<RoleName, string> = {
  chat: 'Answers you. The only role allowed to think.',
  planner: 'Writes the plan for multi-step tasks.',
  classifier: 'Pre-flight: tier, skills, routing; compaction summaries.',
  judge: 'Supervises a run when structural signals fire.',
  triage: 'Classifies inbound mail in the background.',
};
