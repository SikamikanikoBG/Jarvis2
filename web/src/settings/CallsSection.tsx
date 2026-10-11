import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton, RelativeTime } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import { disablePush, enablePush, pushStatus, thisEndpoint, type PushStatus } from '../push/push';
import { useStore } from '../store/store';

const STATUS: Record<PushStatus, string> = {
  on: 'This device rings.',
  off: 'This device does not ring yet.',
  blocked: 'Notifications are blocked for Jarvis in this browser - allow them in the site settings to ring here.',
  unsupported: 'This browser cannot ring (no web push). On an iPhone, add Jarvis to the home screen first.',
};

/**
 * Calls from Jarvis (docs/stories/11_call_me.md): the devices a finished task rings, this one
 * among them, and a test ring. Discord carries the ring when no device takes it.
 */
export function CallsSection() {
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.push.state(), []);
  const { data, error, reload } = useLoader(load, 'devices');
  const [status, setStatus] = useState<PushStatus | null>(null);
  const [mine, setMine] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    void pushStatus().then(setStatus);
    void thisEndpoint().then(setMine);
    reload();
  }, [reload]);
  useEffect(() => {
    void pushStatus().then(setStatus);
    void thisEndpoint().then(setMine);
  }, []);

  const run = (what: Promise<unknown>, failed: string) => {
    setBusy(true);
    what
      .catch((e: unknown) => notify(`${failed}: ${errorText(e)}`, 'error'))
      .finally(() => {
        setBusy(false);
        refresh();
      });
  };
  const ringHere = () => run(enablePush(), 'This device cannot ring');
  const stopHere = () => run(disablePush(), 'Could not stop it');
  const forget = (endpoint: string) => run(api.push.forget(endpoint), 'Could not forget the device');
  const test = () =>
    run(
      api.push.test().then(({ carrier }) =>
        notify(carrier === 'push' ? 'Ringing your devices.' : carrier === 'discord' ? 'No device took it - sent to Discord.' : 'Nothing could carry it: no device rings and Discord is not set up.', carrier ? 'info' : 'error'),
      ),
      'The test ring failed',
    );

  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Calls from Jarvis</h2>
        <p>Turn on the bell when you send a task (or say "call me when you're done") and Jarvis rings when it ends. Answering opens a call that tells you the result.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      {status && <div className="small">{STATUS[status]}</div>}
      <div className="mcp-list">
        {data?.devices.length === 0 && <div className="empty small">No device rings yet{' - '}the ring goes to Discord.</div>}
        {(data?.devices ?? []).map((d) => (
          <div key={d.endpoint} className="mcp-row">
            <Icon name="bell" size={16} className="muted" />
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="row">
                <span style={{ fontWeight: 600 }}>{d.name || 'Device'}</span>
                {d.endpoint === mine && <span className="xs muted">this device</span>}
              </div>
              <div className="xs muted">
                added <RelativeTime ts={d.created_at} /> · {d.last_ok_at ? <>last rang <RelativeTime ts={d.last_ok_at} /></> : 'not rung yet'}
              </div>
            </div>
            <IconButton icon="trash" label={`Stop ringing ${d.name || 'this device'}`} size="sm" disabled={busy} onClick={() => (d.endpoint === mine ? stopHere() : forget(d.endpoint))} />
          </div>
        ))}
        <div className="row">
          {status === 'off' && (
            <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={ringHere}>
              <Icon name="bell" size={14} /> Ring this device
            </button>
          )}
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={test}>
            <Icon name="phone" size={14} /> Test ring
          </button>
        </div>
      </div>
    </section>
  );
}
