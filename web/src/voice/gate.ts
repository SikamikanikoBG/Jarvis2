/**
 * The gate on the microphone while Jarvis speaks.
 *
 * His voice reaches the microphone — through the laptop's speakers, through the phone's — and
 * the browser's echo cancellation does not catch it: `speechSynthesis` plays outside the
 * browser's own audio path, so there is nothing for the canceller to subtract. On the laptop
 * the answer was transcribed as the next question. A fixed multiple of the room's noise floor
 * was not enough either: the echo is far above the floor.
 *
 * So the gate learns the echo. While he speaks it tracks the loudest the microphone has been
 * recently (a peak that decays slowly), and a frame counts as speech only when it is well above
 * that peak — a voice OVER his, not his own. The first moments of each sentence are a grace
 * period, because the peak has to be learned before it can be compared against.
 *
 * Two things it got wrong, both found on 2026-09-19 ("докато ми говори не мога да говоря през
 * него"):
 *
 * - **It learned the cut-in.** The peak followed any frame that was not already over the bar, so
 *   a voice rising into the room raised the bar under itself and never broke through. After the
 *   grace the peak may now only rise slowly (`rise`): the echo, which is already there, keeps it;
 *   a voice arriving into it cannot.
 * - **It kept judging a cut-in that had been accepted.** Once the segmenter has heard 450 ms of
 *   voice over the echo it has decided; from then the gate is `latch`ed open and passes
 *   everything until the utterance closes, so the words are not chopped into fragments too short
 *   to be an utterance at all. That was the second half of "he stops but does not hear me".
 *
 * And when the page itself plays his voice (the server voice through the call's own AudioContext)
 * the browser's echo canceller has already subtracted most of it, so the bar can be far lower:
 * `CANCELLED`, chosen by the session, is what makes cutting in easy rather than merely possible.
 */

export interface GateOptions {
  /** A voice must be this many times louder than the learned echo peak to count. */
  overEcho: number;
  /** The peak decays by this factor per frame (~43 ms): 0.99 ≈ 20% per second. */
  decay: number;
  /** Frames at the start of speaking during which nothing counts: the peak is being learned. */
  graceMs: number;
  /** Below this the echo is treated as at least this loud, so a quiet sentence does not open the gate to noise. */
  floorTimes: number;

  /** After the grace, the peak may rise by at most this per frame (~43 ms). */
  rise: number;
}

/** His voice comes out of a speaker the browser knows nothing about (`speechSynthesis`). */
export const DEFAULT_GATE: GateOptions = { overEcho: 2.5, decay: 0.99, graceMs: 350, floorTimes: 5, rise: 1.06 };
/** The page plays his voice itself: the canceller has it, so a raised voice is plenty. */
export const CANCELLED_GATE: GateOptions = { overEcho: 1.5, decay: 0.99, graceMs: 200, floorTimes: 2.5, rise: 1.06 };

export class EchoGate {
  private peak = 0;
  private speakingSince: number | null = null;
  private latched = false;
  opts: GateOptions;

  constructor(opts: Partial<GateOptions> = {}) {
    this.opts = { ...DEFAULT_GATE, ...opts };
  }

  /** Which bar to judge against: the page's own voice is a far quieter echo than the device's. */
  setProfile(cancelled: boolean, over: Partial<GateOptions> = {}): void {
    this.opts = { ...(cancelled ? CANCELLED_GATE : DEFAULT_GATE), ...over };
  }

  /** The cut-in has been accepted: pass everything until the utterance closes. */
  latch(): void {
    this.latched = true;
  }

  unlatch(): void {
    this.latched = false;
  }

  get open(): boolean {
    return this.latched;
  }

  get echoPeak(): number {
    return this.peak;
  }

  /** Jarvis started or stopped speaking. */
  setSpeaking(on: boolean, now: number, floor: number): void {
    if (on && this.speakingSince === null) {
      this.speakingSince = now;
      this.peak = Math.max(this.peak, floor * this.opts.floorTimes);
    } else if (!on) {
      this.speakingSince = null;
      this.latched = false;
    }
  }

  /**
   * The level to hand the segmenter for this frame: the frame itself when the gate is open or
   * the frame is a voice over the echo, zero otherwise.
   */
  pass(rms: number, now: number, floor: number): number {
    if (this.speakingSince === null) {
      // Not speaking: the peak fades quickly so the next sentence starts from what the room is.
      this.peak *= 0.9;
      return rms;
    }
    if (this.latched) return rms; // decided: he is talking over him, and the rest is his words
    const bar = Math.max(this.peak * this.opts.overEcho, floor * this.opts.floorTimes);
    const inGrace = now - this.speakingSince < this.opts.graceMs;
    const over = !inGrace && rms >= bar;
    // The peak learns from what is NOT a cut-in: a voice over the echo must not raise the bar
    // under itself. Everything else — the echo, its syllables, its pauses — shapes the peak.
    // While the echo is being learned it may rise freely; after that only slowly, or a voice
    // rising into the room takes the bar up with it and can never be over anything.
    if (!over) {
      const ceiling = inGrace ? rms : Math.min(rms, this.peak * this.opts.rise);
      this.peak = Math.max(this.peak * this.opts.decay, ceiling);
    }
    return over ? rms : 0;
  }
}
