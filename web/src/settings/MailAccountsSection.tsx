import { useState } from 'react';
import { api } from '../api/client';
import { Switch } from '../components/primitives';
import type { MailAccount, MailTestResult } from '../protocol/types';

interface Props {
  value: MailAccount[];
  onChange: (v: MailAccount[]) => void;
  error?: string;
}

const GMAIL: MailAccount = {
  address: '',
  app_password: '',
  username: '',
  imap_host: 'imap.gmail.com',
  imap_port: 993,
  smtp_host: 'smtp.gmail.com',
  smtp_port: 465,
  enabled: true,
};

/** Mailboxes the core reaches itself over IMAP/SMTP - independent of Outlook and the Windows host. */
export function MailAccountsSection({ value, onChange, error }: Props) {
  const [tests, setTests] = useState<Record<number, MailTestResult | 'running'>>({});
  const set = (i: number, p: Partial<MailAccount>) => onChange(value.map((a, j) => (j === i ? { ...a, ...p } : a)));
  const test = async (i: number) => {
    setTests((t) => ({ ...t, [i]: 'running' }));
    try {
      const res = await api.mail.test(value[i]);
      setTests((t) => ({ ...t, [i]: res }));
    } catch (e) {
      setTests((t) => ({ ...t, [i]: { ok: false, error: String(e) } }));
    }
  };

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Mail accounts (IMAP) — mail without Outlook</h2>
        <p>
          Jarvis reads, sorts, drafts and sends mail for these accounts itself, straight from the mail server. They keep working when Outlook or the
          Windows host is down. Triage uses this automatically for any account listed here that is also in Triage → accounts.
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}

      <details className="field" open={value.length === 0}>
        <summary>
          <b>How to connect Gmail (5 minutes)</b>
        </summary>
        <ol className="field-hint" style={{ lineHeight: 1.7, paddingLeft: '1.2em' }}>
          <li>
            Turn on <b>2-Step Verification</b> for the Google account (required for app passwords):{' '}
            <a href="https://myaccount.google.com/signinoptions/twosv" target="_blank" rel="noreferrer">
              myaccount.google.com/signinoptions/twosv
            </a>
            .
          </li>
          <li>
            Open{' '}
            <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">
              myaccount.google.com/apppasswords
            </a>{' '}
            (or search “App passwords” in your Google Account). If it says the setting is not available: 2-Step Verification is still off, the
            account is a work/school account whose admin blocks it, or Advanced Protection is on.
          </li>
          <li>
            Type a name, e.g. <code>Jarvis</code>, and press <b>Create</b>.
          </li>
          <li>
            Copy the <b>16-letter password</b> Google shows (spaces do not matter). It is shown only once; if you lose it, delete it and create a new one.
          </li>
          <li>
            Below: <b>Add Gmail account</b>, enter your Gmail address and paste the app password, press <b>Test</b>, then <b>Save</b> at the top of
            the page. Nothing else is needed: IMAP is always on in Gmail.
          </li>
        </ol>
        <div className="field-hint">
          The app password only works for mail (IMAP/SMTP), not for signing in to Google. Revoke it any time on the same page; changing your Google
          password revokes it too. Never paste your normal Google password here.
        </div>
      </details>

      {value.map((acct, i) => {
        const t = tests[i];
        return (
          <div key={i} className="card" style={{ padding: 12, marginTop: 10 }}>
            <div className="form-grid">
              <div className="field">
                <label htmlFor={`mail-addr-${i}`}>Email address</label>
                <input id={`mail-addr-${i}`} className="input mono" value={acct.address} onChange={(e) => set(i, { address: e.target.value.trim() })} placeholder="you@gmail.com" />
              </div>
              <div className="field">
                <label htmlFor={`mail-pw-${i}`}>App password</label>
                <input
                  id={`mail-pw-${i}`}
                  className="input mono"
                  type="password"
                  autoComplete="new-password"
                  value={acct.app_password}
                  onChange={(e) => set(i, { app_password: e.target.value })}
                  placeholder="abcd efgh ijkl mnop"
                />
              </div>
            </div>
            <details className="field">
              <summary className="field-hint">Server settings (only for a provider other than Gmail)</summary>
              <div className="form-grid">
                <div className="field">
                  <label>IMAP server : port</label>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <input className="input mono" value={acct.imap_host} onChange={(e) => set(i, { imap_host: e.target.value.trim() })} />
                    <input className="input mono" style={{ width: 90 }} type="number" value={acct.imap_port} onChange={(e) => set(i, { imap_port: Number(e.target.value) || 993 })} />
                  </div>
                </div>
                <div className="field">
                  <label>SMTP server : port (SSL)</label>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <input className="input mono" value={acct.smtp_host} onChange={(e) => set(i, { smtp_host: e.target.value.trim() })} />
                    <input className="input mono" style={{ width: 90 }} type="number" value={acct.smtp_port} onChange={(e) => set(i, { smtp_port: Number(e.target.value) || 465 })} />
                  </div>
                </div>
                <div className="field">
                  <label>Login name</label>
                  <input className="input mono" value={acct.username} onChange={(e) => set(i, { username: e.target.value.trim() })} placeholder="(same as the address)" />
                </div>
              </div>
            </details>
            <div className="think-row">
              <Switch checked={acct.enabled} onChange={(v) => set(i, { enabled: v })} label="Enabled" />
              <span className="small">Enabled</span>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => void test(i)} disabled={!acct.address || t === 'running'}>
                {t === 'running' ? 'Testing…' : 'Test'}
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onChange(value.filter((_, j) => j !== i))}>
                Remove
              </button>
              {t && t !== 'running' && (
                <span className={t.ok ? 'field-hint' : 'field-error'}>
                  {t.ok ? `✓ Connected — ${t.inbox_messages} messages in Inbox, ${t.folders} folders. Press Save to keep it.` : `✗ ${t.error}`}
                </span>
              )}
            </div>
          </div>
        );
      })}

      <button type="button" className="btn btn-secondary btn-sm" style={{ alignSelf: 'flex-start', marginTop: 10 }} onClick={() => onChange([...value, { ...GMAIL }])}>
        Add Gmail account
      </button>
    </section>
  );
}
