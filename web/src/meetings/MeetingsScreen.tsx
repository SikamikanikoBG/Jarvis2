import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { Highlight } from '../components/Highlight';
import { IconButton, InlineConfirm, RelativeTime } from '../components/primitives';
import { ScreenFilter } from '../components/ScreenFilter';
import { useTicker } from '../components/useTicker';
import { matchesQuery } from '../lib/filter';
import { formatDuration } from '../lib/format';
import { errorText, useLoader } from '../lib/useLoader';
import type { Meeting, MeetingStatus } from '../protocol/types';
import { useStore } from '../store/store';
import { DESKTOP_QUERY, useMediaQuery } from '../shell/useMediaQuery';
import { phoneRecorder, type RecorderState } from './recorder';

/** The host name of a meeting recorded by this browser (core: features/meetings.PHONE_HOST). */
const PHONE = 'phone';

function useRecorder(): RecorderState {
  const [state, setState] = useState(phoneRecorder.snapshot);
  useEffect(() => phoneRecorder.subscribe(setState), []);
  return state;
}

const STATUS_CHIP: Record<MeetingStatus, string> = { recording: 'chip-danger', summarising: 'chip-accent', done: 'chip-ok', failed: 'chip-danger' };

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  return `${m.toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}`;
}

/** Meetings: start on a host, follow the live transcript, stop, read the summary. */
export function MeetingsScreen() {
  const version = useStore((s) => s.featureVersion.meetings);
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.meetings.list(), []);
  const { data, error, loading, reload } = useLoader(load, version);
  const [selected, setSelected] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const [query, setQuery] = useState('');
  const all = [...(data ?? [])].sort((a, b) => b.started_at.localeCompare(a.started_at));
  const q = query.trim();
  const meetings = all.filter((m) => matchesQuery(q, m.title, m.host, m.status));
  const showList = desktop || selected === null;
  const rec = useRecorder();

  // Stop from the notification after Android had closed the page: the app opens with ?stop=.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const id = params.get('stop');
    if (!id) return;
    params.delete('stop');
    window.history.replaceState(null, '', window.location.pathname + (params.size ? `?${params.toString()}` : ''));
    void phoneRecorder.recover().then(() => {
      if (phoneRecorder.snapshot.meetingId === id) void phoneRecorder.stop().then(reload);
    });
  }, [reload]);

  return (
    <div className="screen">
      <div className="screen-inner wide kg-layout">
        {showList && (
          <section className="kg-list">
            <div className="section-head">
              <h1>Meetings</h1>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => setStarting(true)}>
                <Icon name="mic" size={15} />
                Start meeting
              </button>
            </div>
            <RecorderBanner rec={rec} onChanged={reload} onOpen={setSelected} notify={notify} />
            {starting && (
              <StartForm
                onClose={() => setStarting(false)}
                onStarted={(m) => {
                  setStarting(false);
                  reload();
                  setSelected(m.id);
                }}
              />
            )}
            {error && <div className="field-error">{error}</div>}
            {all.length > 0 && (
              <ScreenFilter query={query} onQuery={setQuery} placeholder="Search meetings" shown={meetings.length} total={all.length} noun="meeting" />
            )}
            {!loading && all.length === 0 && !starting && (
              <div className="empty">
                <strong>No meetings recorded</strong>
                <span>Start one: this phone records with its microphone (also with the screen locked), or a host mixes mic and system audio. Jarvis transcribes as it goes and summarises when you stop.</span>
              </div>
            )}
            {!loading && all.length > 0 && meetings.length === 0 && (
              <div className="empty">
                <strong>Nothing matches</strong>
                <span>No meeting matches “{q}”. Titles, hosts and status are searched.</span>
              </div>
            )}
            <div className="kg-entities">
              {meetings.map((m) => (
                <button key={m.id} type="button" className={`kg-entity${selected === m.id ? ' active' : ''}`} onClick={() => setSelected(m.id)}>
                  <span className="row">
                    <span className="kg-name truncate">
                      <Highlight text={m.title} query={q} />
                    </span>
                    <span className={`chip ${STATUS_CHIP[m.status]}`}>
                      {m.status === 'recording' && <span className="dot dot-danger dot-pulse" />}
                      {m.status}
                    </span>
                  </span>
                  <span className="xs muted">
                    <Highlight text={m.host} query={q} /> · <RelativeTime ts={m.started_at} />
                  </span>
                </button>
              ))}
            </div>
          </section>
        )}
        {selected ? (
          <MeetingPanel id={selected} version={version} onBack={() => setSelected(null)} desktop={desktop} onChanged={reload} notify={notify} />
        ) : (
          desktop && (
            <section className="kg-detail empty">
              <strong>Pick a meeting</strong>
              <span>The transcript, captured frames and the summary appear here.</span>
            </section>
          )
        )}
      </div>
    </div>
  );
}

/** What this phone is doing with a meeting: recording it, sending the last pieces, or one the page
 *  was closed in the middle of. */
function RecorderBanner({
  rec,
  onChanged,
  onOpen,
  notify,
}: {
  rec: RecorderState;
  onChanged: () => void;
  onOpen: (id: string) => void;
  notify: (text: string, level?: 'info' | 'error') => void;
}) {
  const now = useTicker(1000, rec.recording);
  if (!rec.meetingId && !rec.pending) return null;
  const id = rec.meetingId;
  return (
    <div className="card role-card" role="status">
      {rec.recording && id && (
        <div className="row">
          <span className="dot dot-danger dot-pulse" />
          <button type="button" className="kg-link grow truncate" onClick={() => onOpen(id)}>
            Recording on this phone: {rec.title} · {formatDuration(now - rec.startedAt)}
          </button>
          <button type="button" className="btn btn-danger btn-sm" onClick={() => void phoneRecorder.stop().then(onChanged)}>
            <Icon name="stop" size={14} />
            Stop
          </button>
        </div>
      )}
      {rec.interrupted && id && (
        <>
          <div className="small">The app was closed while “{rec.title}” was recording. What was recorded is kept.</div>
          <div className="row">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => void phoneRecorder.start({ id, title: rec.title }, true).catch((e: unknown) => notify(errorText(e), 'error'))}
            >
              Continue recording
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => void phoneRecorder.finishInterrupted().then(onChanged)}>
              Finish and summarise
            </button>
          </div>
        </>
      )}
      {rec.finishing && <div className="small muted">Sending the last {rec.pending || ''} piece(s); the summary follows.</div>}
      {!rec.meetingId && rec.pending > 0 && <div className="small muted">Sending {rec.pending} recorded piece(s)…</div>}
      {rec.problem && <div className="small field-error">{rec.problem}</div>}
    </div>
  );
}

function StartForm({ onClose, onStarted }: { onClose: () => void; onStarted: (m: Meeting) => void }) {
  const loadStatus = useCallback(() => api.status(), []);
  const { data: status } = useLoader(loadStatus, 'hosts');
  const hosts = (status?.tools ?? []).filter((p) => p.name !== 'builtin').map((p) => p.name);
  const [title, setTitle] = useState('');
  const [host, setHost] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const chosen = host || PHONE;
  const start = () => {
    const t = title.trim();
    setBusy(true);
    setErr(null);
    api.meetings
      .create(t ? { title: t, host: chosen } : { host: chosen })
      .then(async (m) => {
        if (chosen === PHONE) {
          try {
            await phoneRecorder.start({ id: m.id, title: m.title });
          } catch (e) {
            await api.meetings.remove(m.id).catch(() => undefined);
            throw e;
          }
        }
        onStarted(m);
      })
      .catch((e: unknown) => setErr(errorText(e)))
      .finally(() => setBusy(false));
  };
  return (
    <div className="card role-card">
      <div className="form-grid">
        <div className="field">
          <label>Title</label>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Steering committee" autoFocus />
        </div>
        <div className="field">
          <label>Recorded by</label>
          <select className="select" value={chosen} onChange={(e) => setHost(e.target.value)}>
            <option value={PHONE}>This phone (its microphone)</option>
            {hosts.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
        </div>
      </div>
      {chosen === PHONE && (
        <div className="field-hint">
          Keeps recording with the screen locked. Stop it here or from the notification; allow notifications to get the Stop button there.
        </div>
      )}
      {err && <div className="field-error">{err}</div>}
      <div className="row">
        <button type="button" className="btn btn-primary btn-sm" onClick={start} disabled={busy}>
          {busy ? 'Starting…' : 'Start recording'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  );
}

interface PanelProps {
  id: string;
  version: number;
  onBack: () => void;
  desktop: boolean;
  onChanged: () => void;
  notify: (text: string, level?: 'info' | 'error') => void;
}

function MeetingPanel({ id, version, onBack, desktop, onChanged, notify }: PanelProps) {
  const openConversation = useStore((s) => s.openConversation);
  const live = useStore((s) => s.meetingSegments[id]);
  const load = useCallback(() => api.meetings.get(id), [id]);
  const { data: m, error, reload } = useLoader(load, version);
  const recording = m?.status === 'recording';
  const now = useTicker(1000, recording);
  const segments = useMemo(() => {
    const map = new Map<number, { seq: number; t0: number; t1: number; text: string }>();
    for (const s of m?.segments ?? []) map.set(s.seq, s);
    for (const s of live ?? []) map.set(s.seq, s);
    return [...map.values()].sort((a, b) => a.seq - b.seq);
  }, [m, live]);
  const [stopping, setStopping] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (error) return <section className="kg-detail field-error">{error}</section>;
  if (!m) return <section className="kg-detail empty small">Loading…</section>;

  const stop = () => {
    setStopping(true);
    // Recorded here: the recorder sends its last pieces first, then stops it at the core.
    const request = m.host === PHONE && phoneRecorder.snapshot.meetingId === m.id ? phoneRecorder.stop() : api.meetings.stop(m.id);
    request
      .then(() => {
        reload();
        onChanged();
      })
      .catch((e: unknown) => notify(`Stop failed: ${errorText(e)}`, 'error'))
      .finally(() => setStopping(false));
  };
  const elapsed = recording ? now - Date.parse(m.started_at) : m.ended_at ? Date.parse(m.ended_at) - Date.parse(m.started_at) : null;

  return (
    <section className="kg-detail" aria-label={m.title}>
      <header className="row">
        {!desktop && <IconButton icon="chevronLeft" label="Back to the list" onClick={onBack} />}
        <h2 className="grow truncate" style={{ fontSize: 'var(--fs-xl)' }}>
          {m.title}
        </h2>
        <span className={`chip ${STATUS_CHIP[m.status]}`}>
          {recording && <span className="dot dot-danger dot-pulse" />}
          {m.status}
        </span>
        {recording && (
          <button type="button" className="btn btn-danger btn-sm" onClick={stop} disabled={stopping}>
            <Icon name="stop" size={14} />
            {stopping ? 'Stopping…' : 'Stop'}
          </button>
        )}
        {!confirmDelete && <IconButton icon="trash" label="Delete this recording" onClick={() => setConfirmDelete(true)} />}
      </header>
      {confirmDelete && (
        <InlineConfirm
          text={`Delete "${m.title}", its transcript and its frames?`}
          confirmLabel="Delete"
          danger
          onConfirm={() => {
            setConfirmDelete(false);
            api.meetings
              .remove(m.id)
              .then(() => {
                onBack();
                onChanged();
              })
              .catch((e: unknown) => notify(`Delete failed: ${errorText(e)}`, 'error'));
          }}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
      <div className="row small muted" style={{ flexWrap: 'wrap' }}>
        <span>host {m.host}</span>
        <span>· started {new Date(m.started_at).toLocaleString()}</span>
        {elapsed !== null && <span>· {formatDuration(elapsed)}</span>}
        <button type="button" className="kg-link xs" onClick={() => void openConversation(m.conversation_id)}>
          <Icon name="link" size={11} /> {m.status === 'done' ? 'open summary conversation' : 'open transcript conversation'}
        </button>
      </div>
      <div className="kg-section">
        <div className="field-label">
          Transcript · {segments.length} segments
        </div>
        {segments.length === 0 && <div className="small muted">{recording ? 'Listening… segments appear as they are transcribed.' : 'No transcript.'}</div>}
        <div className="mtg-segments">
          {segments.map((s) => (
            <div key={s.seq} className="mtg-seg">
              <span className="mono xs muted">
                {clock(s.t0)}–{clock(s.t1)}
              </span>
              <span>{s.text}</span>
            </div>
          ))}
        </div>
      </div>
      {m.frames.length > 0 && (
        <div className="kg-section">
          <div className="field-label">
            Frames · {m.frames.length}
          </div>
          <div className="mtg-frames">
            {m.frames.map((f) => (
              <figure key={f.seq} className="mtg-frame" title={f.ocr ?? undefined}>
                <img src={f.url} alt={f.ocr ? `Screen at ${clock(f.at)}: ${f.ocr.slice(0, 80)}` : `Screen at ${clock(f.at)}`} loading="lazy" />
                <figcaption className="xs muted">{clock(f.at)}</figcaption>
              </figure>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
