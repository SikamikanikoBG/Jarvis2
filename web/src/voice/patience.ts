/**
 * How long a call waits for him at a pause that sounds unfinished (`voice.turn_patience_s`), in
 * the milliseconds the session counts in. Read at the tap of the call button, so a change in
 * Settings is the next call's (docs/stories/12_assistant.md). The core validates the setting;
 * this holds it to the same 1-8 s against an older core that has no such key, or anything else
 * that is not a number of seconds.
 */
import type { VoiceSettings } from '../protocol/types';

export const PATIENCE_DEFAULT_MS = 3000;
export const PATIENCE_MIN_MS = 1000;
export const PATIENCE_MAX_MS = 8000;

/** What Settings → Voice offers, in seconds; a value set some other way is offered beside them. */
export const PATIENCE_CHOICES = [1, 1.5, 2, 3, 4, 5, 6, 8];

export function patienceChoices(voice: Partial<Pick<VoiceSettings, 'turn_patience_s'>>): number[] {
  const now = patienceMs(voice) / 1000;
  return PATIENCE_CHOICES.includes(now) ? PATIENCE_CHOICES : [...PATIENCE_CHOICES, now].sort((a, b) => a - b);
}

/** The setting's hint, in seconds, for the way it works now: with the turn model, or without. */
export function patienceHint(voice: Partial<Pick<VoiceSettings, 'turn_patience_s' | 'turn_detection'>>): string {
  const s = patienceMs(voice) / 1000;
  return voice.turn_detection
    ? `When a sentence sounds unfinished, Jarvis waits up to ${s} s of silence for you to go on; a finished one is still sent at once.`
    : `Without the model every pause waits ${s} s of silence before Jarvis answers.`;
}

export function patienceMs(voice: Partial<Pick<VoiceSettings, 'turn_patience_s'>> | null | undefined): number {
  const s = voice?.turn_patience_s;
  if (typeof s !== 'number' || !Number.isFinite(s)) return PATIENCE_DEFAULT_MS;
  return Math.min(PATIENCE_MAX_MS, Math.max(PATIENCE_MIN_MS, Math.round(s * 1000)));
}
