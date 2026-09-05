import { Icon } from '../components/Icon';
import { useTicker } from '../components/useTicker';
import { formatDuration, formatTokens } from '../lib/format';
import { STATUS_LABEL, runDurationMs, statusChipClass } from '../lib/runs';
import { describeThink } from '../lib/think';
import { isTerminal } from '../protocol/types';
import { useStore } from '../store/store';

/** Small per-turn chip: status · steps · tokens · duration. Opens the Run Inspector. */
export function RunChip({ runId }: { runId: string }) {
  const run = useStore((s) => s.runs[runId]);
  const stopping = useStore((s) => s.cancelRequested[runId] === true);
  const openInspector = useStore((s) => s.openInspector);
  const active = run ? !isTerminal(run.status) : false;
  const now = useTicker(1000, active);
  if (!run) return null;
  const dur = runDurationMs(run, now);
  const tokens = run.usage.prompt_tokens + run.usage.completion_tokens;
  const statusText = stopping && active ? 'stopping' : STATUS_LABEL[run.status];
  const think = describeThink(run.think, run.think_level);
  return (
    <div className="runrow">
      <button type="button" className="runchip" onClick={() => openInspector(runId)} aria-label={`Open run inspector: ${statusText}`}>
        <Icon name="activity" size={13} />
        {run.status !== 'done' && <span className={`chip ${statusChipClass(run.status, active)}`}>{statusText}</span>}
        {think && <span className="chip chip-outline">{think}</span>}
        <span>
          {run.steps_used} {run.steps_used === 1 ? 'step' : 'steps'}
        </span>
        {tokens > 0 && (
          <>
            <span className="sep">·</span>
            <span>{formatTokens(tokens)} tok</span>
          </>
        )}
        {dur !== null && (
          <>
            <span className="sep">·</span>
            <span>{formatDuration(dur)}</span>
          </>
        )}
      </button>
    </div>
  );
}
