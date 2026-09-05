import { Switch } from '../components/primitives';
import type { Confirmations, EmailPolicy } from '../protocol/types';

const lines = (s: string) =>
  s
    .split('\n')
    .map((x) => x.trim())
    .filter(Boolean);

/** When a tool call stops for approval. */
export function ConfirmationsSection({ value, onChange, error }: { value: Confirmations; onChange: (v: Confirmations) => void; error?: string }) {
  const patch = (p: Partial<Confirmations>) => onChange({ ...value, ...p });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Confirmations</h2>
        <p>Unattended runs (scheduled, triage, meeting) never ask, whatever is set here.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="field">
        <span className="field-label" id="confirm-mode">
          Mode
        </span>
        <div className="radio-list" role="radiogroup" aria-labelledby="confirm-mode">
          <button type="button" role="radio" aria-checked={value.mode === 'destructive'} className={`radio-card${value.mode === 'destructive' ? ' active' : ''}`} onClick={() => patch({ mode: 'destructive' })}>
            <span className="radio-dot" aria-hidden="true" />
            <span>
              <b>Ask before destructive tools</b>
              <span className="field-hint">Sending mail, creating calendar entries, writing files, running a shell command.</span>
            </span>
          </button>
          <button type="button" role="radio" aria-checked={value.mode === 'off'} className={`radio-card${value.mode === 'off' ? ' active' : ''}`} onClick={() => patch({ mode: 'off' })}>
            <span className="radio-dot" aria-hidden="true" />
            <span>
              <b>Never ask — full autonomy</b>
              <span className="field-hint">Only the supervisor and the per-run budgets bound a bad idea. The always-ask list below still applies.</span>
            </span>
          </button>
        </div>
      </div>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="always-allow">Never ask for (one per line)</label>
          <textarea id="always-allow" className="textarea mono" rows={4} value={value.always_allow.join('\n')} onChange={(e) => patch({ always_allow: lines(e.target.value) })} placeholder={'workocholic.shell_run\nfs.*'} />
          <div className="field-hint">Exact names, `namespace.*`, or `*.tool_name`.</div>
        </div>
        <div className="field">
          <label htmlFor="always-ask">Always ask for (one per line)</label>
          <textarea id="always-ask" className="textarea mono" rows={4} value={value.always_ask.join('\n')} onChange={(e) => patch({ always_ask: lines(e.target.value) })} placeholder="*.outlook_send" />
          <div className="field-hint">Wins over the mode: the last stop before something irreversible.</div>
        </div>
      </div>
    </section>
  );
}

/** Recipients Jarvis may send to without a human in the loop. */
export function EmailSection({ value, onChange, error }: { value: EmailPolicy; onChange: (v: EmailPolicy) => void; error?: string }) {
  const patch = (p: Partial<EmailPolicy>) => onChange({ ...value, ...p });
  const count = value.approved_direct_send.length;
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Email — who Jarvis may write to directly</h2>
        <p>Anyone not listed gets a draft in Outlook instead of a sent message; an empty list drafts everything.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="field">
        <label htmlFor="approved-send">Approved recipients (one per line)</label>
        <textarea id="approved-send" className="textarea mono" rows={4} value={value.approved_direct_send.join('\n')} onChange={(e) => patch({ approved_direct_send: lines(e.target.value) })} placeholder={'rumen@bank.bg\n@postbank.bg'} disabled={value.allow_any_recipient} />
        <div className="field-hint">
          {value.allow_any_recipient ? 'Ignored while the escape hatch below is on.' : count === 0 ? 'Empty: every message is drafted, nothing is sent automatically.' : `${count} entr${count === 1 ? 'y' : 'ies'}. A line starting with @ matches the whole domain.`}
        </div>
      </div>
      <div className="think-row">
        <Switch checked={value.allow_any_recipient} onChange={(v) => patch({ allow_any_recipient: v })} label="Allow any recipient" />
        <span className="small">Allow any recipient</span>
        <span className="field-hint">Turns the policy off entirely — Jarvis may send to anyone. Off by design.</span>
      </div>
    </section>
  );
}
