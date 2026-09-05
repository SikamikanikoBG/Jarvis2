import { RelativeTime } from '../components/primitives';
import { useTicker } from '../components/useTicker';
import { formatDuration, formatTokens } from '../lib/format';
import { runDurationMs, statusChipClass } from '../lib/runs';
import { isTerminal } from '../protocol/types';
import { useStore } from '../store/store';

const EMPTY: never[] = [];

/** Runs of the open conversation, newest first. Tapping one opens the inspector. */
export function RunsScreen() {
  const convId = useStore((s) => s.openConversationId);
  const title = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId]?.title : undefined));
  const runIds = useStore((s) => (s.openConversationId ? (s.runsByConversation[s.openConversationId] ?? EMPTY) : EMPTY));
  const runs = useStore((s) => s.runs);
  const openInspector = useStore((s) => s.openInspector);
  const now = useTicker(1000, runIds.some((id) => runs[id] && !isTerminal(runs[id].status)));

  return (
    <div className="screen">
      <div className="screen-inner">
        <div className="section-head">
          <h1>Runs</h1>
          {title && <p className="truncate">{title}</p>}
        </div>
        {!convId ? (
          <div className="empty">
            <strong>No conversation open</strong>
            <span>Open a chat to see its runs here.</span>
          </div>
        ) : runIds.length === 0 ? (
          <div className="empty">
            <strong>No runs yet</strong>
            <span>Every message you send becomes a run you can inspect.</span>
          </div>
        ) : (
          <div className="runlist">
            {runIds.map((id) => {
              const r = runs[id];
              if (!r) return null;
              const active = !isTerminal(r.status);
              const dur = runDurationMs(r, now);
              const statusClass = statusChipClass(r.status, active);
              return (
                <button key={id} type="button" className="card runitem" onClick={() => openInspector(id)}>
                  <div className="runitem-main">
                    <div className="runitem-title">{r.input_text || `${r.kind} run`}</div>
                    <div className="runitem-meta">
                      <span className={`chip ${statusClass}`}>{r.status.replace('_', ' ')}</span>
                      <span>{r.steps_used} steps</span>
                      <span>{formatTokens(r.usage.prompt_tokens + r.usage.completion_tokens)} tok</span>
                      {dur !== null && <span>{formatDuration(dur)}</span>}
                      <RelativeTime ts={r.created_at} />
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
