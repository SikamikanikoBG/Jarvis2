import type { Run, RunStatus } from '../protocol/types';

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

export function statusChipClass(status: RunStatus, active: boolean): string {
  if (status === 'failed') return 'chip-danger';
  if (status === 'waiting_user') return 'chip-warn';
  return active ? 'chip-accent' : 'chip-outline';
}
