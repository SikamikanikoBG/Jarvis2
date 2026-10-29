/** How a delegated task reads on its card and in the header chip (docs/stories/13_delegated_tasks.md). */
import type { DodResult, TaskState } from '../protocol/types';

/** The words a card says about a task's state (the same as the core's report). */
export const TASK_STATE_LABEL: Readonly<Record<TaskState, string>> = {
  noted: 'noted',
  queued: 'queued',
  running: 'working',
  waiting: 'waiting on you',
  needs_input: 'needs your answer',
  done: 'done',
  partial: 'partly done',
  not_done: 'not done',
  failed: 'failed',
  cancelled: 'cancelled',
  dropped: 'dropped',
};

/** Pill colour: state is the one thing colour is for. */
export function taskTone(state: TaskState): 'run' | 'done' | 'wait' | 'fail' | 'muted' {
  switch (state) {
    case 'queued':
    case 'running':
    case 'noted':
      return 'run';
    case 'done':
      return 'done';
    case 'waiting':
    case 'needs_input':
    case 'partial':
      return 'wait';
    case 'not_done':
    case 'failed':
      return 'fail';
    default:
      return 'muted';
  }
}

/** One DoD line as the code verified it - never as the model claimed it. */
export function dodMark(r: DodResult): { mark: string; label: string } {
  if (r.met === true && r.verified) return { mark: '✅', label: 'done' };
  if (r.met === true) return { mark: '⚠️', label: `claimed, not verified${r.why ? ` (${r.why})` : ''}` };
  if (r.met === false) return { mark: '❌', label: r.note ? `not met: ${r.note}` : 'not met' };
  return { mark: '◻️', label: 'not reported' };
}
