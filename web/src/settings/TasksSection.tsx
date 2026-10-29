import { useState } from 'react';
import { Switch } from '../components/primitives';
import type { TasksSettings } from '../protocol/types';

interface Props {
  value: TasksSettings;
  onChange: (v: TasksSettings) => void;
  error?: string;
}

const list = (text: string) =>
  text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);

/**
 * Delegated tasks (docs/stories/13_delegated_tasks.md): longer work handed to a task of its own
 * and reported back in the chat that asked; on a call, noted and done after it. A task runs with
 * nobody watching, so what it may do without asking is a list here, not a hope.
 */
export function TasksSection({ value, onChange, error }: Props) {
  const patch = (p: Partial<TasksSettings>) => onChange({ ...value, ...p });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Tasks — “on it, I’ll come back to you”</h2>
        <p>
          Longer work goes to a task with a brief and a Definition of Done; a card in the chat shows it and the report comes back
          there. On a call he notes it and starts it when you hang up.
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Delegated tasks" />
        <span className="small">Enabled</span>
        <span className="field-hint">Off: no task tools, and a call does everything on the line, as before.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="tasks-running">Tasks at once</label>
          <select id="tasks-running" className="select" value={String(value.max_running)} onChange={(e) => patch({ max_running: Number(e.target.value) })}>
            <option value="1">1 — the GPU is shared with your chats and calls</option>
            <option value="2">2</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="tasks-ttl">An approval waits</label>
          <select id="tasks-ttl" className="select" value={String(value.approval_ttl_s)} onChange={(e) => patch({ approval_ttl_s: Number(e.target.value) })}>
            <option value="3600">1 hour</option>
            <option value="14400">4 hours</option>
            <option value="86400">a day</option>
          </select>
          <div className="field-hint">Unanswered, it is turned down and the task re-reads its source before it asks again.</div>
        </div>
        <div className="field">
          <label htmlFor="tasks-drop">A silent call page has dropped after</label>
          <select id="tasks-drop" className="select" value={String(value.call_drop_after_s)} onChange={(e) => patch({ call_drop_after_s: Number(e.target.value) })}>
            <option value="120">2 minutes</option>
            <option value="240">4 minutes</option>
            <option value="480">8 minutes</option>
          </select>
          <div className="field-hint">Then what was noted on the call starts. A locked phone wakes its timers about once a minute.</div>
        </div>
      </div>
      <div className="field">
        <label htmlFor="tasks-free">A task may use these without asking</label>
        <ListField id="tasks-free" value={value.no_approval} onChange={(v) => patch({ no_approval: v })} />
        <div className="field-hint">Everything else a task does that is not read-only waits for your OK — whatever the confirmation mode says.</div>
      </div>
      <div className="field">
        <label htmlFor="tasks-deny">Never offered to a task</label>
        <ListField id="tasks-deny" value={value.deny_tools} onChange={(v) => patch({ deny_tools: v })} />
      </div>
    </section>
  );
}

/** Names or globs, comma or newline separated; the text is kept as typed until it is parsed. */
function ListField({ id, value, onChange }: { id: string; value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState(value.join(', '));
  return (
    <textarea
      id={id}
      className="textarea"
      rows={2}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(list(e.target.value));
      }}
    />
  );
}
