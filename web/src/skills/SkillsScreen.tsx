import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { Highlight } from '../components/Highlight';
import { IconButton, InlineConfirm, RelativeTime, Switch } from '../components/primitives';
import { ScreenFilter } from '../components/ScreenFilter';
import { matchesQuery } from '../lib/filter';
import { errorText, useLoader } from '../lib/useLoader';
import type { Skill } from '../protocol/types';
import { useStore } from '../store/store';

const TEMPLATE = (name: string) => `---
name: ${name}
description: One line on when this skill applies.
triggers:
  - keyword
---

# ${name}

Write the instructions Jarvis should follow when this skill is detected.
`;

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

function formatBytes(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} kB`;
}

/** Skill files: list with enable toggles, monospace editor (frontmatter visible), new / delete. */
export function SkillsScreen() {
  const version = useStore((s) => s.featureVersion.skills);
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.skills.list(), []);
  const { data, error, loading, reload } = useLoader(load, version);
  /** null = closed; { name: null } = new skill; { name } = editing an existing one. */
  const [editing, setEditing] = useState<{ name: string | null } | null>(null);
  const [query, setQuery] = useState('');
  const all = [...(data ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  const q = query.trim();
  // Triggers are searched too: what a skill fires on is often how Arsen remembers it.
  const skills = all.filter((s) => matchesQuery(q, s.name, s.description, s.triggers.join(' ')));

  const toggle = (s: Skill, enabled: boolean) => api.skills.patch(s.name, enabled).then(reload).catch((e: unknown) => notify(`Could not update ${s.name}: ${errorText(e)}`, 'error'));

  return (
    <div className="screen">
      <div className={`screen-inner wide skills-layout${editing ? ' editing' : ''}`}>
        <section className="skills-list">
          <div className="section-head">
            <h1>Skills</h1>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing({ name: null })}>
              <Icon name="plus" size={15} />
              New skill
            </button>
          </div>
          <p className="small muted" style={{ margin: 0 }}>
            Markdown with YAML frontmatter. Detected per message by the classifier; only enabled skills are offered.
          </p>
          {error && <div className="field-error">{error}</div>}
          {all.length > 0 && (
            <ScreenFilter query={query} onQuery={setQuery} placeholder="Search skills" shown={skills.length} total={all.length} noun="skill" />
          )}
          {!loading && all.length === 0 && (
            <div className="empty">
              <strong>No skills yet</strong>
              <span>Create one, or import the V1 skills folder.</span>
            </div>
          )}
          {!loading && all.length > 0 && skills.length === 0 && (
            <div className="empty">
              <strong>Nothing matches</strong>
              <span>No skill matches “{q}”. Names, descriptions and triggers are searched.</span>
            </div>
          )}
          <div className="skills-rows">
            {skills.map((s) => (
              <div key={s.name} className={`skill-row${editing?.name === s.name ? ' active' : ''}${s.enabled ? '' : ' disabled'}`}>
                <Switch checked={s.enabled} onChange={(v) => void toggle(s, v)} label={`Enable ${s.name}`} />
                <button type="button" className="skill-main" onClick={() => setEditing({ name: s.name })}>
                  <span className="row">
                    <span className="mono" style={{ fontWeight: 600 }}>
                      <Highlight text={s.name} query={q} />
                    </span>
                    <span className="xs muted">{formatBytes(s.size)}</span>
                  </span>
                  {s.description && (
                    <span className="small muted truncate">
                      <Highlight text={s.description} query={q} />
                    </span>
                  )}
                  {s.triggers.length > 0 && (
                    <span className="row" style={{ flexWrap: 'wrap', gap: 4 }}>
                      {s.triggers.slice(0, 6).map((t) => (
                        <span key={t} className="chip chip-outline">
                          {t}
                        </span>
                      ))}
                      {s.triggers.length > 6 && <span className="xs muted">+{s.triggers.length - 6}</span>}
                    </span>
                  )}
                </button>
                <span className="xs muted">
                  <RelativeTime ts={s.updated_at} />
                </span>
              </div>
            ))}
          </div>
        </section>
        {editing && <SkillEditor key={editing.name ?? '__new__'} name={editing.name} onClose={() => setEditing(null)} onSaved={(name) => (reload(), setEditing({ name }))} />}
      </div>
    </div>
  );
}

function SkillEditor({ name, onClose, onSaved }: { name: string | null; onClose: () => void; onSaved: (name: string) => void }) {
  const notify = useStore((s) => s.notify);
  const [newName, setNewName] = useState('');
  const [content, setContent] = useState<string | null>(name ? null : TEMPLATE('my-skill'));
  const [loaded, setLoaded] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!name) return;
    let alive = true;
    api.skills
      .get(name)
      .then((s) => {
        if (alive) {
          setContent(s.content);
          setLoaded(s.content);
        }
      })
      .catch((e: unknown) => alive && setErr(errorText(e)));
    return () => {
      alive = false;
    };
  }, [name]);

  const target = name ?? newName.trim();
  const nameOk = name !== null || NAME_RE.test(target);
  const dirty = content !== null && content !== loaded;

  const save = () => {
    if (!nameOk || content === null) return;
    setSaving(true);
    setErr(null);
    api.skills
      .put(target, content)
      .then(() => {
        setLoaded(content);
        notify(`Saved ${target}.`);
        onSaved(target);
      })
      .catch((e: unknown) => setErr(errorText(e)))
      .finally(() => setSaving(false));
  };
  const remove = () =>
    api.skills
      .remove(target)
      .then(() => {
        notify(`Deleted ${target}.`);
        onClose();
      })
      .catch((e: unknown) => setErr(errorText(e)));

  return (
    <section className="skill-editor" aria-label={name ? `Edit ${name}` : 'New skill'}>
      <header className="row">
        {name ? (
          <h2 className="mono grow truncate">{name}</h2>
        ) : (
          <input className={`input mono${newName && !nameOk ? ' invalid' : ''}`} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="skill-name" aria-label="Skill name" autoFocus />
        )}
        <IconButton icon="x" label="Close editor" onClick={onClose} />
      </header>
      {!name && newName && !nameOk && <div className="field-error">lowercase letters, digits, - or _ (max 64)</div>}
      {content === null ? (
        <div className="empty small">Loading…</div>
      ) : (
        <textarea className="textarea mono skill-text" value={content} onChange={(e) => setContent(e.target.value)} spellCheck={false} aria-label="Skill content" />
      )}
      {err && <div className="field-error">{err}</div>}
      <div className="row">
        <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={saving || !nameOk || content === null || (!dirty && name !== null)}>
          {saving ? 'Saving…' : name ? 'Save' : 'Create'}
        </button>
        {name && !confirmDelete && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirmDelete(true)}>
            <Icon name="trash" size={14} />
            Delete
          </button>
        )}
        {confirmDelete && <InlineConfirm text={`Delete ${target}?`} confirmLabel="Delete" danger onConfirm={() => void remove()} onCancel={() => setConfirmDelete(false)} />}
        <span className="field-hint grow">{dirty ? 'Unsaved changes' : ''}</span>
      </div>
    </section>
  );
}
