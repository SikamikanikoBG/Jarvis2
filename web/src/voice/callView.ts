/**
 * What the call screen says, as pure functions of the session's state, so they are tested
 * without a DOM (CallScreen.tsx draws them).
 */
import type { CallPhase, CallState } from './session';

const PHASE_LABEL: Record<CallPhase, string> = {
  idle: '',
  connecting: 'Connecting…',
  listening: 'Listening',
  transcribing: 'Heard you…',
  thinking: 'Thinking…',
  speaking: 'Speaking',
  ended: 'Call ended',
};

/** The line under the ring. Waiting for him to finish a thought is still listening - not
 *  "Heard you…", not thinking (2026-11-14) - and says so, so that he knows to go on. */
export function phaseLabel(call: Pick<CallState, 'phase' | 'muted' | 'online' | 'goOn'>): string {
  if (!call.online) return 'No connection — reconnecting…';
  if (call.muted && call.phase === 'listening') return 'Muted';
  if (call.phase === 'listening' && call.goOn) return 'Listening — go on';
  return PHASE_LABEL[call.phase];
}

/** The amber ring that runs down while the turn waits for him: a CSS animation over the whole
 *  wait, started as far into it as the wait already is (a negative delay), so a re-render does
 *  not restart it and a new wait does. */
export function goOnDrain(goOn: { until: number; ms: number }, now: number): { durationMs: number; delayMs: number } {
  const left = Math.max(0, Math.min(goOn.ms, goOn.until - now));
  return { durationMs: Math.max(1, Math.round(goOn.ms)), delayMs: Math.round(left - goOn.ms) };
}
