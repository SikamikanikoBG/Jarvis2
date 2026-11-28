import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { IconButton, InlineConfirm, Switch } from '../components/primitives';
import { setSharing, sharingEnabled } from '../lib/location';
import { getToken } from '../lib/token';
import { errorText, useLoader } from '../lib/useLoader';
import type { LocationSettings, LocationView, Place } from '../protocol/types';
import { useStore } from '../store/store';

interface Props {
  value: LocationSettings;
  onChange: (v: LocationSettings) => void;
  publicUrl: string | null;
  error?: string;
}

function describe(view: LocationView | null): string {
  if (!view?.fix) return 'No location yet - switch sharing on below, on this phone.';
  const where = [view.locality, view.country].filter(Boolean).join(', ');
  const head = view.place ? `At ${view.place}${where ? ` (${where})` : ''}` : where ? `In ${where}` : 'Between towns';
  const when = new Date(view.fix.taken_at).toLocaleString();
  const acc = view.fix.accuracy_m !== null ? ` ±${Math.round(view.fix.accuracy_m)} m,` : '';
  return `${head} -${acc} ${when}, ${view.fix.source === 'owntracks' ? 'OwnTracks' : 'phone'}`;
}

/**
 * Where Arsen is (core features/location.py): what Jarvis is told, this device's own switch, the
 * places he named, and OwnTracks for a position when the app is closed.
 */
export function LocationSection({ value, onChange, publicUrl, error }: Props) {
  const notify = useStore((s) => s.notify);
  const patch = (p: Partial<LocationSettings>) => onChange({ ...value, ...p });
  const [sharing, setSharingState] = useState(sharingEnabled());
  const [tick, setTick] = useState(0);
  const load = useCallback(() => Promise.all([api.location.get(), api.places.list()]), []);
  const { data, reload } = useLoader(load, tick);
  const view = data?.[0] ?? null;
  const places = data?.[1] ?? [];
  const [name, setName] = useState('');
  const [radius, setRadius] = useState(150);
  const [confirm, setConfirm] = useState<string | null>(null);
  const fail = (what: string) => (e: unknown) => notify(`${what} failed: ${errorText(e)}`, 'error');
  const refresh = () => {
    setTick((t) => t + 1);
    reload();
  };

  const toggleSharing = (on: boolean) => {
    void setSharing(on).then((granted) => {
      setSharingState(on && granted);
      if (on && !granted) notify('The browser did not allow location for Jarvis. Allow it in the site settings and try again.', 'error');
      window.setTimeout(refresh, 1500);
    });
  };
  const savePlace = () =>
    api.places
      .create({ name: name.trim(), radius_m: radius })
      .then((p) => {
        notify(`Saved “${p.name}”.`);
        setName('');
        refresh();
      })
      .catch(fail('Save place'));
  const setRadiusOf = (p: Place, r: number) => api.places.patch(p.id, { radius_m: r }).then(refresh).catch(fail('Update'));
  const removePlace = (p: Place) => api.places.remove(p.id).then(refresh).catch(fail('Delete'));
  const forget = () => api.location.forget().then(refresh).catch(fail('Forget'));

  const base = (publicUrl ?? window.location.origin).replace(/\/$/, '');
  const owntracksUrl = `${base}/api/location/owntracks?token=${getToken() ?? '<token>'}`;

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Location — where you are</h2>
        <p>
          While a device shares its position, every turn is told the town, the place you named (“at Home”) and how old the fix is — scheduled runs
          too, with your last known position. Only the last fix is kept, never a trail; towns are looked up offline, and chats for other people and
          private chats never see it.
        </p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="think-row">
        <Switch checked={value.enabled} onChange={(v) => patch({ enabled: v })} label="Use my location" />
        <span className="small">Use my location</span>
        <span className="field-hint">Off: fixes are refused and no turn is told where you are.</span>
      </div>
      <div className="think-row">
        <Switch checked={sharing} onChange={toggleSharing} label="Share this device's location" />
        <span className="small">Share this device’s location</span>
        <span className="field-hint">Per device. The browser asks once; it is sent while Jarvis is open, with each message and when you move.</span>
      </div>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="loc-precision">What Jarvis sees</label>
          <select id="loc-precision" className="select" value={value.precision} onChange={(e) => patch({ precision: e.target.value as LocationSettings['precision'] })}>
            <option value="exact">Exact — town, place and coordinates (nearby, routes, weather)</option>
            <option value="city">City — only the town and the named place</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="loc-stale">Call it out of date after</label>
          <select id="loc-stale" className="select" value={String(value.stale_after_h)} onChange={(e) => patch({ stale_after_h: Number(e.target.value) })}>
            <option value="2">2 hours</option>
            <option value="6">6 hours</option>
            <option value="12">12 hours</option>
            <option value="24">a day</option>
          </select>
        </div>
      </div>
      <div className="row">
        <span className="small">
          <b>Now:</b> {describe(view)}
          {view?.from_home_km != null && view.place !== 'Home' && view.from_home_km >= 1 && ` · ${Math.round(view.from_home_km)} km from Home`}
        </span>
        <span className="grow" />
        {view?.fix && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void forget()}>
            Forget it
          </button>
        )}
      </div>

      <h3 className="small">Places</h3>
      {places.length === 0 && <div className="small muted">None yet. Name where you are now — Home first, so turns can say how far from home you are.</div>}
      <div className="sched-list">
        {places.map((p) => (
          <div key={p.id} className="row">
            <span className="sched-name">{p.name}</span>
            <select className="select" aria-label={`Radius of ${p.name}`} value={String(p.radius_m)} onChange={(e) => void setRadiusOf(p, Number(e.target.value))}>
              {[...new Set([75, 150, 300, 1000, p.radius_m])].sort((a, b) => a - b).map((r) => (
                <option key={r} value={String(r)}>
                  {r >= 1000 ? `${r / 1000} km` : `${r} m`}
                </option>
              ))}
            </select>
            <span className="grow" />
            <IconButton icon="trash" label={`Delete ${p.name}`} size="sm" onClick={() => setConfirm(p.id)} />
            {confirm === p.id && <InlineConfirm text={`Delete “${p.name}”?`} confirmLabel="Delete" danger onConfirm={() => void removePlace(p)} onCancel={() => setConfirm(null)} />}
          </div>
        ))}
      </div>
      <div className="row">
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Home, Office, Gym…" aria-label="Place name" />
        <select className="select" aria-label="Radius" value={String(radius)} onChange={(e) => setRadius(Number(e.target.value))}>
          <option value="75">75 m</option>
          <option value="150">150 m</option>
          <option value="300">300 m</option>
          <option value="1000">1 km</option>
        </select>
        <button type="button" className="btn btn-secondary btn-sm" disabled={!name.trim() || !view?.fix} onClick={() => void savePlace()}>
          Save where I am
        </button>
      </div>

      <details>
        <summary className="small">In the background too — OwnTracks</summary>
        <div className="small muted">
          A web page only knows where you are while it is open. For the rest of the day, install <b>OwnTracks</b> (Android, free, open source):
          Preferences → Connection → Mode <b>HTTP</b>, URL below, then Monitoring “Significant changes”. It posts here only, to your own core.
        </div>
        <input className="input mono" readOnly value={owntracksUrl} onFocus={(e) => e.target.select()} aria-label="OwnTracks URL" />
      </details>
    </section>
  );
}
