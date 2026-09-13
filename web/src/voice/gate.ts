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
}

export const DEFAULT_GATE: GateOptions = { overEcho: 2.5, decay: 0.99, graceMs: 350, floorTimes: 5 };

export class EchoGate {
  private peak = 0;
  private speakingSince: number | null = null;
  readonly opts: GateOptions;

  constructor(opts: Partial<GateOptions> = {}) {
    this.opts = { ...DEFAULT_GATE, ...opts };
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
    const bar = Math.max(this.peak * this.opts.overEcho, floor * this.opts.floorTimes);
    const inGrace = now - this.speakingSince < this.opts.graceMs;
    const over = !inGrace && rms >= bar;
    // The peak learns from what is NOT a cut-in: a voice over the echo must not raise the bar
    // under itself. Everything else — the echo, its syllables, its pauses — shapes the peak.
    if (!over) this.peak = Math.max(this.peak * this.opts.decay, rms);
    return over ? rms : 0;
  }
}
