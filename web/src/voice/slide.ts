/**
 * A slide, not a press (2026-10-14: "да не мога да натискам случайно екрана, когато е допрян до
 * лицето ми"). The locked call screen's controls used to act on a 1.2 s hold by a contact no wider
 * than a fingertip - but many Android phones report no contact size at all, so a cheek resting on
 * a control was a held finger, and the call hung up or switched to the speaker against a face.
 *
 * A slide asks for what a face cannot do: one contact, starting on the thumb, carried sideways
 * across most of the track and not drifting up or down on the way. Pure, so the rules are tested.
 */

/** How much of the track the thumb must cross. */
export const SLIDE_DONE = 0.8;
/** Drift up or down beyond this (CSS px) is not a slide: a face moves the whole contact, not sideways. */
export const SLIDE_MAX_DRIFT = 44;

export interface SlideState {
  /** 0..1 of the way across. */
  progress: number;
  /** The gesture is spoiled (drifted, went backwards past the start); it cannot complete. */
  aborted: boolean;
}

export function slide(start: { x: number; y: number }, now: { x: number; y: number }, travel: number): SlideState {
  if (travel <= 0) return { progress: 0, aborted: true };
  if (Math.abs(now.y - start.y) > SLIDE_MAX_DRIFT) return { progress: 0, aborted: true };
  return { progress: Math.max(0, Math.min(1, (now.x - start.x) / travel)), aborted: false };
}
