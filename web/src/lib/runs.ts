import type { Run, RunScopedEvent, RunStatus } from '../protocol/types';

export const STATUS_LABEL: Record<RunStatus, string> = {
  queued: 'queued',
  running: 'running',
  waiting_user: 'waiting for you',
  cancelling: 'stopping',
  done: 'done',
  failed: 'failed',
  cancelled: 'stopped',
  interrupted: 'interrupted',
};

/** Wall-clock duration of a run: started → finished (or now while it runs). */
export function runDurationMs(run: Run, now: number): number | null {
  const start = run.started_at ? Date.parse(run.started_at) : null;
  if (start === null) return null;
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  return Math.max(0, end - start);
}

/**
 * Aggregate generation throughput of a run: completion tokens over the time the model spent
 * generating them. Preferred source is the `model.done` events (one per call, exact per-call
 * durations); the run head's summed usage is the fallback when events are not loaded.
 * Returns null when there is nothing to divide — the UI omits the segment rather than showing 0.
 */
export function runTokensPerSecond(run: Run | undefined, events: RunScopedEvent[] | undefined): number | null {
  let tokens = 0;
  let ms = 0;
  for (const ev of events ?? []) {
    if (ev.type === 'model.done') {
      tokens += ev.usage.completion_tokens;
      ms += ev.usage.duration_ms;
    }
  }
  if (tokens <= 0 || ms <= 0) {
    tokens = run?.usage.completion_tokens ?? 0;
    ms = run?.usage.duration_ms ?? 0;
  }
  if (tokens <= 0 || ms <= 0) return null;
  return (tokens / ms) * 1000;
}

/** One decimal below 10, none above — "9.4 tok/s", "51 tok/s". */
export function formatTokensPerSecond(tps: number): string {
  return `${tps < 10 ? tps.toFixed(1) : Math.round(tps)} tok/s`;
}

export function statusChipClass(status: RunStatus, active: boolean): string {
  if (status === 'failed') return 'chip-danger';
  if (status === 'waiting_user') return 'chip-warn';
  return active ? 'chip-accent' : 'chip-outline';
}
