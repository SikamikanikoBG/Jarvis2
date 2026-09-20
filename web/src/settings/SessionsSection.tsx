import { Switch } from '../components/primitives';
import type { SessionsSettings } from '../protocol/types';

interface Props {
  value: SessionsSettings;
  onChange: (v: SessionsSettings) => void;
  error?: string;
}

/**
 * Sessions talking to each other: every chat has an @handle made from its title, and Jarvis can
 * write into another one when Arsen asks him to. The guards here are the answer to the one thing
 * that could go wrong — two of his own chats talking to each other with nobody reading.
 */
export function SessionsSection({ value, onChange, error }: Props) {
  const patch = (p: Partial<SessionsSettings>) => onChange({ ...value, ...p });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Sessions — chats that can talk to each other</h2>
        <p>
          Every chat answers to an <b>@handle</b> made from its title. Type “@” in the composer to see them. Jarvis writes to another
          session only when you ask him to — the message wakes it if it is idle, or joins the run if one is already working there.
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Let sessions write to each other" />
        <span className="small">Enabled</span>
        <span className="field-hint">Off: the tools refuse, and @handles are just text. Private chats are never addressable either way.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="sess-hops">Chain limit</label>
          <select id="sess-hops" className="select" value={String(value.max_hops)} onChange={(e) => patch({ max_hops: Number(e.target.value) })} disabled={!value.enabled}>
            <option value="1">1 — it may ask one session, and there it ends</option>
            <option value="2">2 — that session may ask one more</option>
            <option value="3">3 — rarely worth it</option>
          </select>
          <div className="field-hint">A session already in the chain can never be written to again, whatever this says.</div>
        </div>
        <div className="field">
          <label htmlFor="sess-timeout">Wait for a reply</label>
          <select
            id="sess-timeout"
            className="select"
            value={String(value.reply_timeout_s)}
            onChange={(e) => patch({ reply_timeout_s: Number(e.target.value) })}
            disabled={!value.enabled}
          >
            <option value="60">1 minute</option>
            <option value="180">3 minutes</option>
            <option value="600">10 minutes</option>
          </select>
          <div className="field-hint">After this he says it is still working rather than holding the run open.</div>
        </div>
      </div>
    </section>
  );
}
