import { useEffect, useMemo, useState } from 'react';
import { ApiError, api, describeError, fieldErrors } from '../api/client';
import { Switch } from '../components/primitives';
import type { ThemePref } from '../lib/theme';
import { LANES, ROLE_NAMES, RUN_KINDS, THINK_LEVELS, type Lane, type ModelSpec, type Provider, type RoleName, type RunKind, type Settings, type SttKind, type ThinkLevel, type ToolExposure } from '../protocol/types';
import { useStore } from '../store/store';
import { CollabSection } from './CollabSection';
import { McpServersSection } from './McpServersSection';
import { PersonalitySection } from './PersonalitySection';
import { ConfirmationsSection, EmailSection } from './SafetySections';
import { RsvpSection } from './RsvpSection';
import { SettingsNav } from './SettingsNav';
import { TriageSection } from './TriageSection';
import { SessionsSection } from './SessionsSection';
import { VoiceSection } from './VoiceSection';

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
  const [namespaces, setNamespaces] = useState<string[]>([]);
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
    // The namespaces a call may be allowed: read from the live tool list, so the allow-list
    // offers what exists and never asks Arsen to type "workocholic" from memory.
    api.tools
      .list()
      .then((tools) => setNamespaces([...new Set(tools.map((t) => t.name.split('.', 1)[0] ?? ''))].filter(Boolean)))
      .catch(() => undefined);
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

        <SettingsNav />

        <section id="settings-appearance" className="card role-card settings-anchor">
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

        <section id="settings-general" className="card role-card settings-anchor">
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
            <Field label="Public URL" error={errors.public_url} hint="How the phone reaches this core (pairing/QR). https for mic + PWA.">
              <input className="input" value={draft.public_url ?? ''} onChange={(e) => patch('public_url', e.target.value || null)} placeholder="https://jarvis.tailnet.ts.net" />
            </Field>
            <Field label="STT URL" error={errors.stt_url} hint="Whisper endpoint; empty disables voice input.">
              <input className="input" value={draft.stt_url ?? ''} onChange={(e) => patch('stt_url', e.target.value || null)} placeholder="http://…" />
            </Field>
            <Field label="STT API" error={errors.stt_kind} hint="openai = /v1/audio/transcriptions; asr = WhisperX /asr.">
              <select className="select" value={draft.stt_kind} onChange={(e) => patch('stt_kind', e.target.value as SttKind)}>
                <option value="openai">openai</option>
                <option value="asr">asr</option>
              </select>
            </Field>
            <Field label="STT model" error={errors.stt_model}>
              <input className="input" value={draft.stt_model} onChange={(e) => patch('stt_model', e.target.value)} placeholder="large-v3" />
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

        <div id="settings-personality" className="settings-anchor">
          <PersonalitySection value={draft.personality} onChange={(p) => patch('personality', p)} error={errors.personality} />
        </div>

        <div id="settings-voice" className="settings-anchor">
          <VoiceSection value={draft.voice} namespaces={namespaces} onChange={(v) => patch('voice', v)} error={errors.voice} />
        </div>

        <div id="settings-sessions" className="settings-anchor">
          <SessionsSection value={draft.sessions} onChange={(v) => patch('sessions', v)} error={errors.sessions} />
        </div>

        <div id="settings-confirmations" className="settings-anchor">
          <ConfirmationsSection value={draft.confirmations} onChange={(c) => patch('confirmations', c)} error={errors.confirmations} />
        </div>

        <div id="settings-email" className="settings-anchor">
          <EmailSection value={draft.email} onChange={(e) => patch('email', e)} error={errors.email} />
        </div>

        <section id="settings-behaviour" className="card role-card settings-anchor">
          <div className="section-head">
            <h2>Context and behaviour</h2>
            <p>What goes into the prompt, and how tools are exposed to the model.</p>
          </div>
          <div className="think-row">
            <Switch checked={draft.planning_enabled} onChange={(v) => patch('planning_enabled', v)} label="Planning" />
            <span className="small">Planning</span>
            <span className="field-hint">Multi-step tasks get a plan the model advances step by step.</span>
          </div>
          <div className="think-row">
            <Switch checked={draft.lane_failover} onChange={(v) => patch('lane_failover', v)} label="Lane failover" />
            <span className="small">Lane failover</span>
            <span className="field-hint">A run whose lane cannot be reached retries the step once on the other lane.</span>
          </div>
          <div className="think-row">
            <Switch checked={draft.kg_learning} onChange={(v) => patch('kg_learning', v)} label="Knowledge learning" />
            <span className="small">Knowledge learning</span>
            <span className="field-hint">After each run the classifier extracts entities and relations into Knowledge.</span>
          </div>
          <div className="form-grid">
            <Field label="Tool exposure" error={errors.tool_exposure} hint="facade = one tool per namespace; flat = every tool; auto switches at the threshold.">
              <select className="select" value={draft.tool_exposure} onChange={(e) => patch('tool_exposure', e.target.value as ToolExposure)}>
                <option value="auto">auto</option>
                <option value="flat">flat</option>
                <option value="facade">facade</option>
              </select>
            </Field>
            <Field label="Facade threshold (tools)" error={errors.facade_threshold}>
              <NumberInput value={draft.facade_threshold} min={1} onChange={(v) => patch('facade_threshold', v ?? 1)} />
            </Field>
            <Field label="History budget (tokens)" hint="Earlier turns a step may carry; shrinks with the lane's window." error={errors.history_token_budget}>
              <NumberInput value={draft.history_token_budget} min={1000} onChange={(v) => patch('history_token_budget', v ?? 1000)} />
            </Field>
            <Field label="Tool results budget (tokens)" hint="This run's tool results before the oldest shrink to heads." error={errors.tool_context_token_budget}>
              <NumberInput value={draft.tool_context_token_budget} min={1000} onChange={(v) => patch('tool_context_token_budget', v ?? 1000)} />
            </Field>
            <Field label="Admit a tool result up to (chars)" hint="Longer results enter as a head + ref; result_search / result_read reach the rest. Capped at half the results budget on a small lane." error={errors.tool_result_admit_chars}>
              <NumberInput value={draft.tool_result_admit_chars} min={2000} onChange={(v) => patch('tool_result_admit_chars', v ?? 2000)} />
            </Field>
            <Field label="Context reserve (tokens)" hint="Kept free of the lane's window besides the answer." error={errors.context_reserve_tokens}>
              <NumberInput value={draft.context_reserve_tokens} min={0} onChange={(v) => patch('context_reserve_tokens', v ?? 0)} />
            </Field>
            <Field label="Pictures and clips in context" hint="Newest first; older ones stay as a note. Keep under the lane's per-prompt limit (6 images)." error={errors.media_in_context}>
              <NumberInput value={draft.media_in_context} min={0} onChange={(v) => patch('media_in_context', v ?? 0)} />
            </Field>
            <Field label="Tools jarvis.wait_until may poll" hint="Comma-separated, on top of every read-only tool. A namespace glob works (homelab.*). Never put anything that sends here." error={errors.wait_until_pollable}>
              <input className="input mono" value={draft.wait_until_pollable.join(', ')} onChange={(e) => patch('wait_until_pollable', e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} placeholder="fetch.fetch, homelab.*" />
            </Field>
            <Field label="Boards in context (chars)" error={errors.boards_context_chars}>
              <NumberInput value={draft.boards_context_chars} min={0} onChange={(v) => patch('boards_context_chars', v ?? 0)} />
            </Field>
            <Field label="Skill size limit (chars)" error={errors.skill_max_chars}>
              <NumberInput value={draft.skill_max_chars} min={500} onChange={(v) => patch('skill_max_chars', v ?? 500)} />
            </Field>
          </div>
        </section>

        <section id="settings-roles" className="settings-anchor" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="section-head">
            <h2>Model roles</h2>
            <p>
              chat and background are the two lanes: a run is routed to one of them whole. The other roles are behaviours —
              inside a run they execute on the run&apos;s lane; their own endpoint only serves calls made outside any run. No
              fallbacks: a missing model stays missing.
            </p>
          </div>
          {errors.roles && <div className="field-error">{errors.roles}</div>}
          {ROLE_NAMES.map((role) => (
            <RoleCard key={role} role={role} spec={draft.roles[role]} onChange={(s) => patchRole(role, s)} />
          ))}
        </section>

        <section id="settings-routing" className="card role-card settings-anchor">
          <div className="section-head">
            <h2>Run routing</h2>
            <p>Which lane each kind of run executes on. Keep the chat you are typing in away from the runs that read 80k tokens a step.</p>
          </div>
          {errors.run_routing && <div className="field-error">{errors.run_routing}</div>}
          <div className="form-grid">
            {RUN_KINDS.map((kind) => (
              <Field key={kind} label={kind}>
                <select
                  className="select"
                  value={draft.run_routing?.[kind] ?? 'chat'}
                  onChange={(e) => patch('run_routing', { ...draft.run_routing, [kind]: e.target.value as Lane })}
                  aria-label={`${kind} lane`}
                >
                  {LANES.map((lane) => (
                    <option key={lane} value={lane}>
                      {lane}
                    </option>
                  ))}
                </select>
              </Field>
            ))}
          </div>
        </section>

        <div id="settings-mcp" className="settings-anchor">
          <McpServersSection servers={draft.mcp_servers} onChange={(list) => patch('mcp_servers', list)} error={errors.mcp_servers} />
        </div>

        <div id="settings-triage" className="settings-anchor">
          <TriageSection value={draft.triage} hosts={draft.mcp_servers.map((s) => s.name)} onChange={(t) => patch('triage', t)} error={errors.triage} />
        </div>

        <div id="settings-rsvp" className="settings-anchor">
          <RsvpSection value={draft.rsvp} hosts={draft.mcp_servers.map((s) => s.name)} onChange={(r) => patch('rsvp', r)} error={errors.rsvp} />
        </div>

        <div id="settings-collab" className="settings-anchor">
          <CollabSection />
        </div>

        <section id="settings-budgets" className="card role-card settings-anchor">
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
  const mayThink = role === 'chat' || role === 'background';
  const ollama = spec.provider === 'ollama';
  return (
    <div id={`role-${role}`} className="card role-card settings-anchor">
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
  chat: 'Lane for the runs you watch (see Run routing). Thinks.',
  background: 'Lane for unattended runs — the same loop as chat, on the endpoint that holds long contexts. Thinks.',
  planner: 'Writes the plan for multi-step tasks. Runs on the lane of the run it plans for.',
  classifier: 'Pre-flight: tier, skills, routing; compaction summaries. Runs on the lane of the run it serves.',
  judge: 'Supervises a run when structural signals fire. Runs on the lane of that run.',
  triage: 'Classifies inbound mail. Runs on the lane triage runs are routed to.',
};
