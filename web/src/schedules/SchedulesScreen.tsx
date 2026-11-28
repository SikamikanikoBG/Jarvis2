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
import type { CatchUp, Project, Schedule, ScheduleCallMe, ScheduleFire } from '../protocol/types';
import { useStore } from '../store/store';
import { groupByProject } from './projects';
import { TriggersSection } from './TriggersSection';

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

const OPEN_KEY = 'jarvis.schedules.open';

/** Scheduled prompts, grouped by the project each serves: list, create/edit, run now, fires. */
export function SchedulesScreen() {
  const version = useStore((s) => s.featureVersion.schedules);
  const notify = useStore((s) => s.notify);
  const openConversation = useStore((s) => s.openConversation);
  const load = useCallback(() => Promise.all([api.schedules.list(), api.projects.list().catch(() => [] as Project[])]), []);
  const { data, error, loading, reload } = useLoader(load, version);
  const [editing, setEditing] = useState<Schedule | 'new' | null>(null);
  const [newIn, setNewIn] = useState<string | null>(null);
  const [editingProject, setEditingProject] = useState<Project | 'new' | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmProject, setConfirmProject] = useState<string | null>(null);
  // Sections start closed - the screen opens as the list of projects - and the ones he opens are
  // remembered on this device (a convenience: without storage they simply start closed again).
  const [open, setOpen] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem(OPEN_KEY);
      return raw ? (JSON.parse(raw) as string[]) : [];
    } catch {
      return [];
    }
  });
  const [query, setQuery] = useState('');
  const [activeOnly, setActiveOnly] = useState(false);
  const now = useTicker(30_000);
  const projects = data?.[1] ?? [];
  const projectName = new Map(projects.map((p) => [p.id, p.name]));
  const all = [...(data?.[0] ?? [])].sort((a, b) => (a.next_fire ?? '9').localeCompare(b.next_fire ?? '9'));
  const q = query.trim();
  // The prompt is searched too: a schedule is named once and then remembered by what it does.
  // Its project's name too: "FX" finds the whole FX project.
  const schedules = all.filter(
    (s) =>
      (!activeOnly || s.enabled) &&
      matchesQuery(q, s.name, s.prompt, s.cron, s.tz, s.last_status, s.project_id ? projectName.get(s.project_id) : null),
  );
  const enabledCount = all.filter((s) => s.enabled).length;
  const sections = groupByProject(schedules, projects, q !== '' || activeOnly);

  const fail = (what: string) => (e: unknown) => notify(`${what} failed: ${errorText(e)}`, 'error');
  const toggle = (s: Schedule, enabled: boolean) => api.schedules.patch(s.id, { enabled }).then(reload).catch(fail('Update'));
  /** Pause or resume every schedule of a project in one go. */
  const toggleAll = (list: Schedule[], enabled: boolean) =>
    Promise.all(list.filter((s) => s.enabled !== enabled).map((s) => api.schedules.patch(s.id, { enabled })))
      .then(reload)
      .catch(fail(enabled ? 'Resume' : 'Pause'));
  const runNow = (s: Schedule) =>
    api.schedules
      .run(s.id)
      .then((r) => {
        notify(`Fired “${s.name}”.`);
        void openConversation(r.conversation_id);
      })
      .catch(fail('Run now'));
  const remove = (id: string) => api.schedules.remove(id).then(reload).catch(fail('Delete'));
  const removeProject = (id: string) => api.projects.remove(id).then(reload).catch(fail('Delete project'));
  const accept = (s: Schedule) => api.schedules.accept(s.id).then(reload).catch(fail('Accept'));
  const fold = (key: string) =>
    setOpen((o) => {
      const next = o.includes(key) ? o.filter((k) => k !== key) : [...o, key];
      try {
        localStorage.setItem(OPEN_KEY, JSON.stringify(next));
      } catch {
        // storage blocked: the choice lasts until the screen closes
      }
      return next;
    });

  const row = (s: Schedule) =>
    editing !== 'new' && editing?.id === s.id ? (
      <ScheduleForm key={s.id} schedule={s} projects={projects} onClose={() => setEditing(null)} onSaved={reload} />
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
              {s.call_me === 'never' && <span className="chip chip-outline">no call</span>}
              {/* A prompt a run wrote fires asking like a task until he accepts it (docs/stories/14_agent_admin.md). */}
              {s.prompt_by && (
                <button type="button" className="chip chip-warn" title="Written by a run, not by you: it asks before acting until you accept it" onClick={() => void accept(s)}>
                  by Jarvis · Accept
                </button>
              )}
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
    );

  return (
    <div className="screen">
      <div className="screen-inner">
        <div className="section-head">
          <h1>Projects</h1>
          <div className="row">
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditingProject('new')}>
              <Icon name="folderPlus" size={15} />
              New project
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                setNewIn(null);
                setEditing('new');
              }}
            >
              <Icon name="plus" size={15} />
              New schedule
            </button>
          </div>
        </div>
        {error && <div className="field-error">{error}</div>}
        {all.length > 0 && (
          <ScreenFilter
            query={query}
            onQuery={setQuery}
            placeholder="Search schedules and projects"
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
        {editingProject === 'new' && <ProjectForm onClose={() => setEditingProject(null)} onSaved={reload} />}
        {editing === 'new' && newIn === null && <ScheduleForm projects={projects} onClose={() => setEditing(null)} onSaved={reload} />}
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
        <div className="proj-list">
          {sections.map(({ project, schedules: list }) => {
            const key = project?.id ?? 'loose';
            // Never folded while searching: a match must not hide behind a closed head. Nor when it
            // is the only section, or while a new schedule is being added to it.
            const isFolded = !open.includes(key) && !q && !activeOnly && sections.length > 1 && !(editing === 'new' && newIn === key);
            const on = list.filter((s) => s.enabled).length;
            const failed = list.filter((s) => s.last_status === 'failed').length;
            const next = list.find((s) => s.enabled && s.next_fire)?.next_fire;
            const addingHere = editing === 'new' && newIn !== null && newIn === project?.id;
            if (project && editingProject !== 'new' && editingProject?.id === project.id) {
              return <ProjectForm key={key} project={project} onClose={() => setEditingProject(null)} onSaved={reload} />;
            }
            return (
              <section key={key} className={`proj-section${project ? '' : ' proj-loose'}`} aria-label={project?.name ?? 'Schedules on their own'}>
                <div className="proj-head">
                  <button type="button" className="proj-toggle" aria-expanded={!isFolded} onClick={() => fold(key)}>
                    <Icon name={isFolded ? 'chevronRight' : 'chevronDown'} size={15} />
                    <span className="proj-name truncate">{project ? <Highlight text={project.name} query={q} /> : 'On their own'}</span>
                  </button>
                  <span className="chip chip-outline" title="Enabled / all schedules in it">
                    {on}/{list.length} on
                  </span>
                  {failed > 0 && <span className="chip chip-danger">{failed} failed</span>}
                  {project?.ends_at && (
                    <span className="chip chip-outline" title="Its schedules pause themselves then">
                      ends {new Date(project.ends_at).toLocaleDateString()}
                    </span>
                  )}
                  {next && <span className="small muted proj-next">next {formatUntil(next, now)}</span>}
                  <span className="grow" />
                  {project && (
                    <>
                      {list.length > 0 && (
                        <IconButton
                          icon={on > 0 ? 'pause' : 'play'}
                          label={on > 0 ? `Pause every schedule in ${project.name}` : `Resume every schedule in ${project.name}`}
                          size="sm"
                          onClick={() => void toggleAll(list, on === 0)}
                        />
                      )}
                      <IconButton
                        icon="plus"
                        label={`New schedule in ${project.name}`}
                        size="sm"
                        onClick={() => {
                          setNewIn(project.id);
                          setEditing('new');
                        }}
                      />
                      <IconButton icon="edit" label={`Edit project ${project.name}`} size="sm" onClick={() => setEditingProject(project)} />
                      <IconButton icon="trash" label={`Delete project ${project.name}`} size="sm" onClick={() => setConfirmProject(project.id)} />
                    </>
                  )}
                </div>
                {project && confirmProject !== null && confirmProject === project.id ? (
                  <InlineConfirm
                    text={`Delete the project "${project.name}"? Its ${list.length} schedule(s) stay, on their own.`}
                    confirmLabel="Delete project"
                    danger
                    onConfirm={() => void removeProject(project.id)}
                    onCancel={() => setConfirmProject(null)}
                  />
                ) : null}
                {!isFolded && project?.goal && <div className="small muted proj-goal">{project.goal}</div>}
                {!isFolded && (
                  <div className="sched-list">
                    {addingHere && <ScheduleForm projects={projects} projectId={newIn} onClose={() => setEditing(null)} onSaved={reload} />}
                    {list.map(row)}
                    {project && list.length === 0 && !addingHere && (
                      <div className="small muted">No schedules yet: add one with +, or move one here from its edit form.</div>
                    )}
                  </div>
                )}
              </section>
            );
          })}
          <TriggersSection projects={projects} version={version} />
        </div>
      </div>
    </div>
  );
}

function ProjectForm({ project, onClose, onSaved }: { project?: Project; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(project?.name ?? '');
  const [goal, setGoal] = useState(project?.goal ?? '');
  const [brief, setBrief] = useState(project?.brief ?? '');
  const [ends, setEnds] = useState(toLocalInput(project?.ends_at ?? null));
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const endsOk = ends === '' || !Number.isNaN(Date.parse(ends));
  const valid = name.trim() !== '' && endsOk;

  const save = () => {
    if (!valid) return;
    const body: Partial<Project> = {
      name: name.trim(),
      goal: goal.trim(),
      brief: brief.trim(),
      ends_at: ends ? new Date(ends).toISOString() : null,
    };
    setSaving(true);
    setErr(null);
    (project ? api.projects.patch(project.id, body) : api.projects.create(body))
      .then(() => {
        onSaved();
        onClose();
      })
      .catch((e: unknown) => setErr(errorText(e)))
      .finally(() => setSaving(false));
  };

  return (
    <div className="card role-card sched-form" aria-label={project ? `Edit project ${project.name}` : 'New project'}>
      <div className="form-grid">
        <div className="field">
          <label>Project name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="FX trading" autoFocus />
        </div>
        <div className="field">
          <label>Ends (optional)</label>
          <input className={`input${endsOk ? '' : ' invalid'}`} type="datetime-local" value={ends} onChange={(e) => setEnds(e.target.value)} />
          <div className="field-hint">Its schedules pause themselves then. Empty = open-ended.</div>
        </div>
        <div className="field span-2">
          <label>Goal</label>
          <textarea className="textarea" rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="What its schedules achieve together" />
        </div>
        <div className="field span-2">
          <label>Brief for every schedule in it (optional)</label>
          <textarea
            className="textarea"
            rows={4}
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            placeholder="Instructions each of its runs is given: where the files are, which playbooks to read, how to deliver"
          />
          <div className="field-hint">Each run in the project also sees the other schedules and what they last reported.</div>
        </div>
      </div>
      {err && <div className="field-error">{err}</div>}
      <div className="row">
        <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={!valid || saving}>
          {saving ? 'Saving…' : project ? 'Save' : 'Create project'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          Cancel
        </button>
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

function ScheduleForm({
  schedule,
  projects,
  projectId,
  onClose,
  onSaved,
}: {
  schedule?: Schedule;
  projects: Project[];
  projectId?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const notify = useStore((s) => s.notify);
  const [name, setName] = useState(schedule?.name ?? '');
  const [prompt, setPrompt] = useState(schedule?.prompt ?? '');
  const [mode, setMode] = useState<'cron' | 'once'>(schedule?.at && !schedule.cron ? 'once' : 'cron');
  const [cron, setCron] = useState(schedule?.cron ?? '0 7 * * 1-5');
  const [at, setAt] = useState(toLocalInput(schedule?.at ?? null));
  const [tz, setTz] = useState(schedule?.tz ?? 'Europe/Sofia');
  const [catchUp, setCatchUp] = useState<CatchUp>(schedule?.catch_up ?? 'skip');
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);
  const [callMe, setCallMe] = useState<ScheduleCallMe>(schedule?.call_me ?? 'default');
  const [thinkKey, setThinkKey] = useState(thinkChoiceKey({ think: schedule?.think ?? null, think_level: schedule?.think_level ?? null }));
  const [project, setProject] = useState(schedule?.project_id ?? projectId ?? '');
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
      call_me: callMe,
      think: think.think,
      think_level: think.think ? think.think_level : null,
      project_id: project || null,
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
          <label>Project</label>
          <select className="select" value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">On its own</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>If a fire was missed</label>
          <select className="select" value={catchUp} onChange={(e) => setCatchUp(e.target.value as CatchUp)}>
            <option value="skip">Skip it</option>
            <option value="run_once">Run once on start-up</option>
          </select>
        </div>
        <div className="field">
          <label>Call me when done</label>
          <select className="select" value={callMe} onChange={(e) => setCallMe(e.target.value as ScheduleCallMe)}>
            <option value="default">As in Settings › Calls</option>
            <option value="always">Always</option>
            <option value="never">Never</option>
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
