import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { Switch } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import type { GarminSettings } from '../protocol/types';
import { useStore } from '../store/store';

interface Props {
  value: GarminSettings;
  onChange: (v: GarminSettings) => void;
  error?: string;
}

const KEY_PAGE = 'https://connect.garmin.com/app/settings/personalApiToken';

/**
 * His Garmin watch (core features/garmin.py): the Personal API key, how often to pull, how long to
 * keep, and what Jarvis sees today.
 */
export function GarminSection({ value, onChange, error }: Props) {
  const notify = useStore((s) => s.notify);
  const patch = (p: Partial<GarminSettings>) => onChange({ ...value, ...p });
  const [tick, setTick] = useState(0);
  const load = useCallback(() => api.garmin.status(), []);
  const { data: status } = useLoader(load, tick);
  const [syncing, setSyncing] = useState(false);
  const sync = () => {
    setSyncing(true);
    api.garmin
      .sync()
      .then((r) => notify(`Garmin: ${r.stored} answers stored${r.failed.length ? `, ${r.failed.length} failed` : ''}.`))
      .catch((e: unknown) => notify(`Garmin sync failed: ${errorText(e)}`, 'error'))
      .finally(() => {
        setSyncing(false);
        setTick((t) => t + 1);
      });
  };

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Garmin — your watch</h2>
        <p>
          Sleep, Body Battery, stress, heart rate, HRV, steps and activities, pulled from Garmin Connect. Every turn of yours hears how you are today, so
          Jarvis can weigh your plans against how rested you are. It stays on this server; chats for other people and private chats never see it.
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Use my Garmin data" />
        <span className="small">Use my Garmin data</span>
      </div>
      <div className="form-grid">
        <div className="field span-2">
          <label htmlFor="garmin-key">Personal API key</label>
          <input
            id="garmin-key"
            className="input mono"
            type="password"
            autoComplete="off"
            value={value.api_key}
            onChange={(e) => patch({ api_key: e.target.value.trim() })}
            placeholder="Paste the key Garmin shows once"
          />
          <div className="field-hint">
            Make one at{' '}
            <a href={KEY_PAGE} target="_blank" rel="noreferrer">
              Garmin Connect → Settings → Personal API
            </a>{' '}
            (Create Key, pick how long it lasts, copy it - it is shown only once), paste it here and Save.
          </div>
        </div>
        <div className="field">
          <label htmlFor="garmin-every">Pull every</label>
          <select id="garmin-every" className="select" value={String(value.sync_every_min)} onChange={(e) => patch({ sync_every_min: Number(e.target.value) })}>
            <option value="15">15 minutes</option>
            <option value="30">30 minutes</option>
            <option value="60">hour</option>
          </select>
          <div className="field-hint">The watch reaches Garmin through the phone app every few minutes to an hour.</div>
        </div>
        <div className="field">
          <label htmlFor="garmin-keep">Keep days</label>
          <select id="garmin-keep" className="select" value={String(value.keep_days)} onChange={(e) => patch({ keep_days: Number(e.target.value) })}>
            <option value="30">30</option>
            <option value="90">90</option>
            <option value="365">365</option>
          </select>
          <div className="field-hint">For “worse than your normal”: averages over the last 30 days.</div>
        </div>
      </div>
      <div className="row">
        <span className="small grow">
          {!status
            ? 'Loading…'
            : !status.configured
              ? 'Not connected - no key saved yet.'
              : status.last_sync
                ? `Last pull ${new Date(status.last_sync).toLocaleString()} · ${status.days_stored} days kept`
                : 'Connected - the first pull runs within a minute of a restart, or now.'}
          {status?.last_error && <span className="field-error"> · {status.last_error}</span>}
        </span>
        <button type="button" className="btn btn-secondary btn-sm" disabled={!status?.configured || syncing} onClick={sync}>
          {syncing ? 'Pulling…' : 'Sync now'}
        </button>
      </div>
      {status?.today && <pre className="small muted garmin-today">{status.today}</pre>}
    </section>
  );
}
