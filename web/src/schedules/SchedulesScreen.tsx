import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { Highlight } from '../components/Highlight';
import { IconButton, InlineConfirm, RelativeTime, Switch } from '../components/primitives';
import { ScreenFilter } from '../components/ScreenFilter';
import { useTicker } from '../components/useTicker';
import { describeCron, isValidCron } from '../lib/cron';
import { matchesQuery } from '../lib/filter';
import { formatDuration } from '../lib/format';
import { THINK_CHOICES, thinkChoiceKey } from '../lib/think';
import { errorText, useLoader } from '../lib/useLoader';
import type { CatchUp, Schedule, ScheduleFire } from '../protocol/types';
import { useStore } from '../store/store';

function formatUntil(ts: string, now: number): string {
  const diff = Date.parse(ts) - now;
  if (Number.isNaN(diff)) return '';
  if (diff <= 0) return 'due';
  return `in ${formatDuration(diff)}`;
}

function toLocalInput(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const p = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function statusChip(status: string | null): string {
  if (!status) return 'chip-outline';
  if (status === 'done') return 'chip-ok';
  if (status === 'failed') return 'chip-danger';
  if (status === 'running' || status === 'queued') return 'chip-accent';
  return 'chip-outline';
}

/** Scheduled prompts: list, create/edit, run now, fires. */
export function SchedulesScreen() {
  const version = useStore((s) => s.featureVersion.schedules);
  const notify = useStore((s) => s.notify);
  const openConversation = useStore((s) => s.openConversation);
  const load = useCallback(() => api.schedules.list(), []);
  const { data, error, loading, reload } = useLoader(load, version);
  const [editing, setEditing] = useState<Schedule | 'new' | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [activeOnly, setActiveOnly] = useState(false);
  const now = useTicker(30_000);
  const all = [...(data ?? [])].sort((a, b) => (a.next_fire ?? '9').localeCompare(b.next_fire ?? '9'));
  const q = query.trim();
  // The prompt is searched too: a schedule is named once and then remembered by what it does.
  const schedules = all.filter(
    (s) => (!activeOnly || s.enabled) && matchesQuery(q, s.name, s.prompt, s.cron, s.tz, s.last_status),
  );
  const enabledCount = all.filter((s) => s.enabled).length;

  const fail = (what: string) => (e: unknown) => notify(`${what} failed: ${errorText(e)}`, 'error');
  const toggle = (s: Schedule, enabled: boolean) => api.schedules.patch(s.id, { enabled }).then(reload).catch(fail('Update'));
  const runNow = (s: Schedule) =>
    api.schedules
      .run(s.id)
      .then((r) => {
        notify(`Fired “${s.name}”.`);
        void openConversation(r.conversation_id);
      })
      .catch(fail('Run now'));
  const remove = (id: string) => api.schedules.remove(id).then(reload).catch(fail('Delete'));

  return (
    <div className="screen">
      <div className="screen-inner">
        <div className="section-head">
          <h1>Schedules</h1>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing('new')}>
            <Icon name="plus" size={15} />
            New schedule
          </button>
        </div>
        {error && <div className="field-error">{error}</div>}
        {all.length > 0 && (
          <ScreenFilter
            query={query}
            onQuery={setQuery}
            placeholder="Search schedules"
            shown={schedules.length}
            total={all.length}
            noun="schedule"
            toggles={[
              {
                label: 'Active only',
                active: activeOnly,
                count: enabledCount,
                title: 'Only the schedules that are enabled and will fire',
                onChange: setActiveOnly,
              },
            ]}
          />
        )}
        {editing === 'new' && <ScheduleForm onClose={() => setEditing(null)} onSaved={reload} />}
        {!loading && all.length === 0 && editing !== 'new' && (
          <div className="empty">
            <strong>No schedules</strong>
            <span>A schedule is a prompt that fires on a cron or once at a time; each fire opens its own conversation under Scheduled.</span>
          </div>
        )}
        {!loading && all.length > 0 && schedules.length === 0 && (
          <div className="empty">
            <strong>Nothing matches</strong>
            <span>
              {activeOnly && q ? `No active schedule matches “${q}”.` : activeOnly ? 'No schedule is active.' : `No schedule matches “${q}”.`}
            </span>
          </div>
        )}
        <div className="sched-list">
          {schedules.map((s) =>
            editing !== 'new' && editing?.id === s.id ? (
              <ScheduleForm key={s.id} schedule={s} onClose={() => setEditing(null)} onSaved={reload} />
            ) : (
              <div key={s.id} className={`card sched-row${s.enabled ? '' : ' disabled'}`}>
                <div className="sched-main">
                  <Switch checked={s.enabled} onChange={(v) => void toggle(s, v)} label={`Enable ${s.name}`} />
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="row">
                      <span className="sched-name truncate">
                        <Highlight text={s.name} query={q} />
                      </span>
                      {s.last_status && <span className={`chip ${statusChip(s.last_status)}`}>{s.last_status}</span>}
                    </div>
                    {/* Only when the prompt is what matched — otherwise the row stays as it was. */}
                    {q && !matchesQuery(q, s.name) && matchesQuery(q, s.prompt) && (
                      <div className="small muted truncate">
                        <Highlight text={s.prompt} query={q} />
                      </div>
                    )}
                    <div className="small muted">
                      {s.cron ? describeCron(s.cron) : s.at ? `Once at ${new Date(s.at).toLocaleString()}` : '—'} · {s.tz}
                      {s.next_fire && s.enabled && (
                        <>
                          {' '}
                          · next {formatUntil(s.next_fire, now)}
                        </>
                      )}
                    </div>
                  </div>
                  <IconButton icon="play" label={`Run ${s.name} now`} size="sm" onClick={() => void runNow(s)} />
                  <IconButton icon="edit" label={`Edit ${s.name}`} size="sm" onClick={() => setEditing(s)} />
                  <IconButton icon="clock" label="Fires" size="sm" active={expanded === s.id} onClick={() => setExpanded(expanded === s.id ? null : s.id)} />
                  <IconButton icon="trash" label={`Delete ${s.name}`} size="sm" onClick={() => setConfirmDelete(s.id)} />
                </div>
                {confirmDelete === s.id && <InlineConfirm text={`Delete "${s.name}"? Past conversations stay.`} confirmLabel="Delete" danger onConfirm={() => void remove(s.id)} onCancel={() => setConfirmDelete(null)} />}
                {expanded === s.id && <FiresList schedule={s} version={version} />}
              </div>
            ),
          )}
        </div>
      </div>
    </div>
  );
}

function FiresList({ schedule, version }: { schedule: Schedule; version: number }) {
  const openConversation = useStore((s) => s.openConversation);
  const load = useCallback(() => api.schedules.fires(schedule.id, 20), [schedule.id]);
  const { data, error } = useLoader(load, version);
  if (error) return <div className="field-error">{error}</div>;
  if (!data) return <div className="small muted">Loading fires…</div>;
  if (data.length === 0) return <div className="small muted sched-fires">Never fired.</div>;
  return (
    <div className="sched-fires">
      {data.map((f: ScheduleFire) => (
        <div key={f.scheduled_for} className="sched-fire">
          <span className="mono small">{new Date(f.scheduled_for).toLocaleString()}</span>
          <span className={`chip ${statusChip(f.status)}`}>{f.status ?? 'pending'}</span>
          <span className="grow" />
          {f.conversation_id && (
            <button type="button" className="kg-link xs" onClick={() => void openConversation(f.conversation_id)}>
              <Icon name="link" size={11} /> open
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function ScheduleForm({ schedule, onClose, onSaved }: { schedule?: Schedule; onClose: () => void; onSaved: () => void }) {
  const notify = useStore((s) => s.notify);
  const [name, setName] = useState(schedule?.name ?? '');
  const [prompt, setPrompt] = useState(schedule?.prompt ?? '');
  const [mode, setMode] = useState<'cron' | 'once'>(schedule?.at && !schedule.cron ? 'once' : 'cron');
  const [cron, setCron] = useState(schedule?.cron ?? '0 7 * * 1-5');
  const [at, setAt] = useState(toLocalInput(schedule?.at ?? null));
  const [tz, setTz] = useState(schedule?.tz ?? 'Europe/Sofia');
  const [catchUp, setCatchUp] = useState<CatchUp>(schedule?.catch_up ?? 'skip');
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);
  const [thinkKey, setThinkKey] = useState(thinkChoiceKey({ think: schedule?.think ?? null, think_level: schedule?.think_level ?? null }));
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const cronOk = mode === 'cron' ? isValidCron(cron) : true;
  const atOk = mode === 'once' ? at !== '' && !Number.isNaN(Date.parse(at)) : true;
  const valid = name.trim() && prompt.trim() && cronOk && atOk;

  const save = () => {
    if (!valid) return;
    const think = THINK_CHOICES.find((c) => c.key === thinkKey)?.choice ?? { think: null, think_level: null };
    const body: Partial<Schedule> = {
      name: name.trim(),
      prompt: prompt.trim(),
      cron: mode === 'cron' ? cron.trim() : null,
      at: mode === 'once' ? new Date(at).toISOString() : null,
      tz: tz.trim() || 'Europe/Sofia',
      enabled,
      catch_up: catchUp,
      think: think.think,
      think_level: think.think ? think.think_level : null,
    };
    setSaving(true);
    setErr(null);
    (schedule ? api.schedules.patch(schedule.id, body) : api.schedules.create(body))
      .then((saved) => {
        notify(saved.next_fire ? `Saved. Next fire ${new Date(saved.next_fire).toLocaleString()}.` : 'Saved.');
        onSaved();
        onClose();
      })
      .catch((e: unknown) => setErr(errorText(e)))
      .finally(() => setSaving(false));
  };

  return (
    <div className="card role-card sched-form" aria-label={schedule ? `Edit ${schedule.name}` : 'New schedule'}>
      <div className="form-grid">
        <div className="field">
          <label>Name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Morning brief" autoFocus />
        </div>
        <div className="field">
          <label>Timezone</label>
          <input className="input" value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Europe/Sofia" />
        </div>
        <div className="field span-2">
          <label>Prompt</label>
          <textarea className="textarea" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="What Jarvis should do when this fires" />
        </div>
        <div className="field">
          <label>When</label>
          <div className="seg" role="radiogroup" aria-label="Schedule type">
            <button type="button" role="radio" aria-checked={mode === 'cron'} className={mode === 'cron' ? 'active' : ''} onClick={() => setMode('cron')}>
              Repeating (cron)
            </button>
            <button type="button" role="radio" aria-checked={mode === 'once'} className={mode === 'once' ? 'active' : ''} onClick={() => setMode('once')}>
              Once
            </button>
          </div>
        </div>
        {mode === 'cron' ? (
          <div className="field">
            <label>Cron (min hour dom mon dow)</label>
            <input className={`input mono${cronOk ? '' : ' invalid'}`} value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 7 * * 1-5" />
            <div className={cronOk ? 'field-hint' : 'field-error'}>{describeCron(cron)}</div>
          </div>
        ) : (
          <div className="field">
            <label>At</label>
            <input className="input" type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} />
            <div className="field-hint">Fires once, then disables itself.</div>
          </div>
        )}
        <div className="field">
          <label>If a fire was missed</label>
          <select className="select" value={catchUp} onChange={(e) => setCatchUp(e.target.value as CatchUp)}>
            <option value="skip">Skip it</option>
            <option value="run_once">Run once on start-up</option>
          </select>
        </div>
        <div className="field">
          <label>Thinking</label>
          <select className="select" value={thinkKey} onChange={(e) => setThinkKey(e.target.value)}>
            {THINK_CHOICES.map((c) => (
              <option key={c.key} value={c.key}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Enabled</label>
          <div className="row" style={{ minHeight: 38 }}>
            <Switch checked={enabled} onChange={setEnabled} label="Enabled" />
          </div>
        </div>
      </div>
      {err && <div className="field-error">{err}</div>}
      <div className="row">
        <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={!valid || saving}>
          {saving ? 'Saving…' : schedule ? 'Save' : 'Create'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          Cancel
        </button>
        {schedule?.next_fire && (
          <span className="field-hint">
            Next fire <RelativeTime ts={schedule.next_fire} /> ago-style times refresh after save.
          </span>
        )}
      </div>
    </div>
  );
}
