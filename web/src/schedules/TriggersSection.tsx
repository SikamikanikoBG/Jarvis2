import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { IconButton, InlineConfirm, Switch } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import type { Place, Project, Trigger, TriggerFire } from '../protocol/types';
import { useStore } from '../store/store';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function when(t: Trigger, places: Place[]): string {
  if (t.source === 'webhook') return 'When its webhook is called';
  const place = places.find((p) => p.id === t.place_id)?.name ?? 'a place';
  if (t.event === 'arrive') return `When you arrive at ${place}`;
  if (t.event === 'leave') return `When you leave ${place}`;
  if (t.event === 'dwell') return `After ${t.dwell_min ?? '?'} min at ${place}`;
  return `When you are on your way to ${place}`;
}

function what(t: Trigger): string {
  if (t.action.kind === 'notify') return `tell you: “${t.action.text ?? ''}”`;
  if (t.action.kind === 'schedule') return 'run a schedule';
  return `ask Jarvis: “${(t.action.prompt ?? '').slice(0, 80)}${(t.action.prompt ?? '').length > 80 ? '…' : ''}”`;
}

function windowText(t: Trigger): string {
  const w = t.window;
  if (!w) return '';
  const days = w.days.length === 7 ? 'every day' : w.days.map((d) => DAYS[d]).join(' ');
  return ` · ${days} ${w.start}–${w.end}`;
}

/**
 * Triggers (core features/triggers.py): webhooks Jarvis made for the code he writes, and location
 * events. Each with why it last fired or did not - the guards are the point of the feature.
 */
export function TriggersSection({ projects, version }: { projects: Project[]; version: number }) {
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => Promise.all([api.triggers.list(), api.places.list().catch(() => [] as Place[])]), []);
  const { data, reload } = useLoader(load, version);
  const [open, setOpen] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const triggers = data?.[0] ?? [];
  const places = data?.[1] ?? [];
  if (triggers.length === 0) return null;
  const fail = (w: string) => (e: unknown) => notify(`${w} failed: ${errorText(e)}`, 'error');
  const projectName = (id: string | null) => projects.find((p) => p.id === id)?.name;
  const copy = (url: string) =>
    navigator.clipboard
      .writeText(url)
      .then(() => notify('Webhook URL copied.'))
      .catch(() => notify(url));

  return (
    <section className="proj-section" aria-label="Triggers">
      <div className="proj-head">
        <span className="proj-name">Triggers</span>
        <span className="chip chip-outline">{triggers.filter((t) => t.enabled && !t.created_by).length} active</span>
      </div>
      <div className="small muted proj-goal">
        Webhooks Jarvis made for his scripts, and events at your places. Each has a cooldown, a daily cap and a chain check; more than 20 firings in an
        hour pause them all.
      </div>
      <div className="sched-list">
        {triggers.map((t) => (
          <div key={t.id} className={`card sched-row${t.enabled ? '' : ' disabled'}`}>
            <div className="sched-main">
              <Switch
                checked={t.enabled}
                onChange={(v) => void api.triggers.patch(t.id, { enabled: v }).then(reload).catch(fail('Update'))}
                label={`Enable ${t.name}`}
              />
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="row">
                  <span className="sched-name truncate">{t.name}</span>
                  {projectName(t.project_id) && <span className="chip chip-outline">{projectName(t.project_id)}</span>}
                  {t.created_by && (
                    <button
                      type="button"
                      className="chip chip-warn"
                      title="Made by a run nobody watched: it cannot fire until you accept it"
                      onClick={() => void api.triggers.accept(t.id).then(reload).catch(fail('Accept'))}
                    >
                      by Jarvis · Accept
                    </button>
                  )}
                </div>
                <div className="small muted">
                  {when(t, places)} → {what(t)}
                  {windowText(t)}
                </div>
                <div className="small muted">
                  Today {t.fired_today}/{t.daily_cap}
                  {t.last_fired_at && ` · last ${new Date(t.last_fired_at).toLocaleString()}`} · cooldown {Math.round(t.cooldown_s / 60)} min
                </div>
              </div>
              {t.webhook_url && <IconButton icon="copy" label={`Copy the webhook URL of ${t.name}`} size="sm" onClick={() => void copy(t.webhook_url ?? '')} />}
              <IconButton icon="clock" label="Why it fired or not" size="sm" active={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)} />
              <IconButton icon="trash" label={`Delete ${t.name}`} size="sm" onClick={() => setConfirm(t.id)} />
            </div>
            {confirm === t.id && (
              <InlineConfirm
                text={`Delete “${t.name}”? ${t.webhook_url ? 'Its webhook URL stops working.' : ''}`}
                confirmLabel="Delete"
                danger
                onConfirm={() => void api.triggers.remove(t.id).then(reload).catch(fail('Delete'))}
                onCancel={() => setConfirm(null)}
              />
            )}
            {open === t.id && <FiresList id={t.id} version={version} />}
          </div>
        ))}
      </div>
    </section>
  );
}

function FiresList({ id, version }: { id: string; version: number }) {
  const openConversation = useStore((s) => s.openConversation);
  const load = useCallback(() => api.triggers.fires(id, 20), [id]);
  const { data } = useLoader(load, version);
  if (!data) return <div className="small muted">Loading…</div>;
  if (data.length === 0) return <div className="small muted sched-fires">Never fired.</div>;
  return (
    <div className="sched-fires">
      {data.map((f: TriggerFire, i) => (
        <div key={`${f.at}-${i}`} className="sched-fire">
          <span className="mono small">{new Date(f.at).toLocaleString()}</span>
          <span className={`chip ${f.outcome === 'fired' ? 'chip-ok' : f.outcome === 'failed' ? 'chip-danger' : 'chip-outline'}`}>{f.outcome}</span>
          <span className="small muted truncate grow">{f.why}</span>
          {f.conversation_id && (
            <button type="button" className="kg-link xs" onClick={() => void openConversation(f.conversation_id)}>
              open
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
