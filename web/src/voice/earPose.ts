import { useEffect, useState } from 'react';

/**
 * Is the phone at an ear? (docs/stories/10_voice.md)
 *
 * The dialer knows from the proximity sensor; a web page is not allowed to read it. What it
 * can read is gravity, through the accelerometer, and the two poses differ in it plainly:
 *
 * - looking at the screen: the phone is tilted back so the glass faces the eyes — a good part
 *   of gravity comes out of the screen (|z| large);
 * - at the ear: the glass lies against a cheek, which is vertical — gravity runs along the
 *   phone's length (|y| large) and almost none comes out of the screen (|z| small).
 *
 * Signs are ignored on purpose: Android and iOS disagree on them, and a phone held upside down
 * at the ear is still at the ear. Hysteresis keeps a pose from flickering at the boundary: the
 * ear is entered only well inside its region and left as soon as the tilt says "looking".
 */
export interface Gravity {
  x: number;
  y: number;
  z: number;
}

/** Enter the ear pose: within ~35° of upright, glass within ~12° of vertical. */
const EAR_UPRIGHT_MIN = 7.0;
const EAR_FACING_MAX = 2.0;
/** Leave it: the glass tilted more than ~22° toward a face, or the phone more than ~48° from upright. */
const AWAY_FACING_MIN = 3.6;
const AWAY_UPRIGHT_MAX = 6.5;

/** The next pose given the gravity reading and the pose we are in. Pure, for the tests. */
export function classifyPose(g: Gravity, atEar: boolean): boolean {
  const upright = Math.abs(g.y);
  const facing = Math.abs(g.z);
  if (atEar) return !(facing >= AWAY_FACING_MIN || upright <= AWAY_UPRIGHT_MAX);
  return upright >= EAR_UPRIGHT_MIN && facing <= EAR_FACING_MAX;
}

/** A pose must hold this long before the screen goes dark; a cheek brushing past is not an ear. */
export const ENTER_MS = 450;
/** …and this long before it comes back: pulling the phone away should feel immediate. */
export const LEAVE_MS = 120;
/** With no reading for this long the sensor is gone; a dark screen must never be stuck dark. */
export const STALE_MS = 2000;

/**
 * Debounced pose tracking over a stream of readings. `feed` returns the current pose; `tick`
 * lets time pass with no reading (the sensor has stopped) and clears the ear.
 */
export class EarTracker {
  private atEar = false;
  private candidate: boolean | null = null;
  private since = 0;
  private lastReading = 0;

  feed(g: Gravity, now: number): boolean {
    this.lastReading = now;
    const next = classifyPose(g, this.atEar);
    if (next === this.atEar) {
      this.candidate = null;
      return this.atEar;
    }
    if (this.candidate !== next) {
      this.candidate = next;
      this.since = now;
      return this.atEar;
    }
    if (now - this.since >= (next ? ENTER_MS : LEAVE_MS)) {
      this.atEar = next;
      this.candidate = null;
    }
    return this.atEar;
  }

  tick(now: number): boolean {
    if (this.atEar && now - this.lastReading > STALE_MS) {
      this.atEar = false;
      this.candidate = null;
    }
    return this.atEar;
  }
}

/**
 * iOS hands out motion readings only after a permission asked for in a tap; Android just
 * delivers them. Call this inside the tap that starts the call; it is a no-op elsewhere.
 */
export async function requestMotionPermission(): Promise<void> {
  const dm = (globalThis as { DeviceMotionEvent?: { requestPermission?: () => Promise<string> } }).DeviceMotionEvent;
  if (!dm?.requestPermission) return;
  try {
    await dm.requestPermission();
  } catch {
    /* refused: the call goes on with the lock alone */
  }
}

/** True while the phone is at an ear, as far as gravity can tell; always false when `on` is false. */
export function useAtEar(on: boolean): boolean {
  const [atEar, setAtEar] = useState(false);
  useEffect(() => {
    if (!on || typeof window === 'undefined' || !('DeviceMotionEvent' in window)) return;
    const tracker = new EarTracker();
    const onMotion = (e: DeviceMotionEvent) => {
      const a = e.accelerationIncludingGravity;
      if (a?.x == null || a.y == null || a.z == null) return;
      setAtEar(tracker.feed({ x: a.x, y: a.y, z: a.z }, performance.now()));
    };
    const stale = window.setInterval(() => setAtEar(tracker.tick(performance.now())), 500);
    window.addEventListener('devicemotion', onMotion);
    return () => {
      window.removeEventListener('devicemotion', onMotion);
      window.clearInterval(stale);
      setAtEar(false);
    };
  }, [on]);
  return on && atEar;
}
