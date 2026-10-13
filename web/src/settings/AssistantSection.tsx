import { Switch } from '../components/primitives';
import type { AssistantSettings } from '../protocol/types';

interface Props {
  value: AssistantSettings;
  onChange: (v: AssistantSettings) => void;
  error?: string;
}

/**
 * Jarvis as one assistant across his chats (docs/stories/12_assistant.md): after a run ends he
 * writes where that conversation stands today, and a call — and the Jarvis conversation — carries
 * the whole day, so "anything else today?" is answered from every chat, not only the open one.
 */
export function AssistantSection({ value, onChange, error }: Props) {
  const patch = (p: Partial<AssistantSettings>) => onChange({ ...value, ...p });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Assistant — the day across all chats</h2>
        <p>
          After each run Jarvis writes one line on where that conversation stands today, and what it waits on you for. A call, and the
          pinned <b>Jarvis</b> conversation, carry those lines; any chat can ask for them (sessions.today).
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.journal} onChange={(v) => patch({ journal: v })} label="Keep the day's journal" />
        <span className="small">Journal</span>
        <span className="field-hint">Written in the background after a run, never in front of a reply. Private chats are never in it.</span>
      </div>
      <div className="think-row">
        <Switch checked={value.overview_on_calls} onChange={(v) => patch({ overview_on_calls: v })} label="Calls know the day" />
        <span className="small">On calls</span>
        <span className="field-hint">Every call turn carries the day's lines; a block that has not changed costs one line, not the whole block.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="as-size">How much of the day</label>
          <select id="as-size" className="select" value={String(value.overview_chars)} onChange={(e) => patch({ overview_chars: Number(e.target.value) })}>
            <option value="1200">Short (~1,200 characters)</option>
            <option value="2400">Normal (~2,400)</option>
            <option value="4000">Long (~4,000)</option>
          </select>
          <div className="field-hint">What does not fit is named by its @handle, not dropped.</div>
        </div>
        <div className="field">
          <label htmlFor="as-lane">Background work runs on</label>
          <select
            id="as-lane"
            className="select"
            value={value.housekeeping_lane}
            onChange={(e) => patch({ housekeeping_lane: e.target.value === 'chat' ? 'chat' : 'background' })}
          >
            <option value="background">The background lane</option>
            <option value="chat">The chat lane</option>
          </select>
          <div className="field-hint">The journal and the chat summaries. The background lane keeps them out of a reply's way.</div>
        </div>
      </div>
    </section>
  );
}
