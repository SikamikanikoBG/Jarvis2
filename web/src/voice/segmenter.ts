/**
 * End-of-utterance detection over energy frames: where one thing Arsen said ends and the next
 * begins, so speech is sent by the sentence rather than by a button.
 *
 * Pure: it is fed `(rms, t)` samples and answers with events. The floor adapts — a headset in a
 * quiet room and a phone on a café table have very different silence — by tracking the quietest
 * recent level and calling speech anything well above it.
 */

export interface SegmenterOptions {
  /** How far above the noise floor counts as speech, as a multiplier. */
  ratio: number;
  /** An absolute minimum for "speech", so a dead-silent room does not turn breathing into words. */
  minLevel: number;
  /** Speech must hold this long before an utterance is open (a click is not a word). */
  attackMs: number;
  /** The floor listens for this long before anything may count as speech: the first frames of
   *  a café are the café, not a voice. */
  warmupMs: number;
  /** Silence must hold this long before the utterance is closed (a pause for breath is not the end). */
  releaseMs: number;
  /** Silence this long is reported as a `pause` — maybe the end, maybe a breath — so the words
   *  so far can be recognised while the release is still being waited out. */
  pauseMs: number;
  /** Utterances shorter than this are dropped (a cough is not a sentence). */
  minUtteranceMs: number;
  /** A ceiling on one utterance; beyond it the utterance is closed and a new one opens. */
  maxUtteranceMs: number;
}

export const DEFAULT_SEGMENTER: SegmenterOptions = {
  ratio: 2.5,
  minLevel: 0.012,
  attackMs: 200,
  warmupMs: 600,
  releaseMs: 750,
  pauseMs: 250,
  minUtteranceMs: 400,
  maxUtteranceMs: 30_000,
};

export type SegmentEvent =
  | { type: 'speech-start'; at: number }
  | { type: 'speech-end'; at: number; startedAt: number; durationMs: number }
  /** Silence has held `pauseMs` since `at`; a `speech-end` that follows without more speech carries the same `at`. */
  | { type: 'pause'; at: number }
  | { type: 'dropped'; at: number; durationMs: number };

type Phase = 'silence' | 'attack' | 'speech' | 'release';

export class Segmenter {
  private phase: Phase = 'silence';
  private phaseSince = 0;
  private utteranceStart = 0;
  private floor: number;
  /** Speech is judged against the floor as it was when the utterance began: a voice that gets
   *  louder as it goes must not raise the floor under itself. */
  private floorAtStart: number;
  private firstAt: number | null = null;
  private pauseSent = false;
  /** A longer attack while a cut-in is what is being listened for (null = the option's). */
  private attackOverride: number | null = null;
  readonly opts: SegmenterOptions;

  constructor(opts: Partial<SegmenterOptions> = {}) {
    this.opts = { ...DEFAULT_SEGMENTER, ...opts };
    this.floor = this.opts.minLevel;
    this.floorAtStart = this.floor;
  }

  get speaking(): boolean {
    return this.phase === 'speech' || this.phase === 'release';
  }

  get noiseFloor(): number {
    return this.floor;
  }

  setAttack(ms: number | null): void {
    this.attackOverride = ms;
  }

  /** Feed one energy frame (rms in 0..1, t in ms). */
  push(rms: number, t: number): SegmentEvent[] {
    const out: SegmentEvent[] = [];
    const o = this.opts;
    if (this.firstAt === null) {
      this.firstAt = t;
      this.floor = Math.max(rms, this.opts.minLevel);
    }
    const warming = t - this.firstAt < o.warmupMs;
    if (this.phase === 'silence' || this.phase === 'attack') {
      // The floor follows the quiet down quickly and up slowly, so a burst does not become
      // "quiet" — except while warming up, when whatever is there IS the room.
      const up = warming ? 0.3 : 0.005;
      this.floor = rms < this.floor ? this.floor * 0.8 + rms * 0.2 : this.floor * (1 - up) + rms * up;
      this.floor = Math.max(this.floor, 0.0005);
    }
    const threshold = Math.max(o.minLevel, (this.speaking || this.phase === 'attack' ? this.floorAtStart : this.floor) * o.ratio);
    const loud = !warming && rms >= threshold;

    switch (this.phase) {
      case 'silence':
        if (loud) {
          this.phase = 'attack';
          this.phaseSince = t;
          this.floorAtStart = this.floor;
        }
        break;
      case 'attack':
        if (!loud) {
          this.phase = 'silence';
        } else if (t - this.phaseSince >= (this.attackOverride ?? o.attackMs)) {
          this.phase = 'speech';
          this.utteranceStart = this.phaseSince;
          out.push({ type: 'speech-start', at: this.utteranceStart });
        }
        break;
      case 'speech':
        if (!loud) {
          this.phase = 'release';
          this.phaseSince = t;
          this.pauseSent = false;
        } else if (t - this.utteranceStart >= o.maxUtteranceMs) {
          out.push(...this.close(t));
          this.phase = 'attack';
          this.phaseSince = t;
        }
        break;
      case 'release':
        if (loud) {
          this.phase = 'speech';
        } else if (t - this.phaseSince >= o.releaseMs) {
          out.push(...this.close(this.phaseSince));
          this.phase = 'silence';
        } else if (!this.pauseSent && t - this.phaseSince >= o.pauseMs && this.phaseSince - this.utteranceStart >= o.minUtteranceMs) {
          this.pauseSent = true;
          out.push({ type: 'pause', at: this.phaseSince });
        }
        break;
    }
    return out;
  }

  /** The stream stopped (the call ended, the mic was lost): close whatever is open. */
  end(t: number): SegmentEvent[] {
    if (!this.speaking) {
      this.phase = 'silence';
      return [];
    }
    const at = this.phase === 'release' ? this.phaseSince : t;
    const out = this.close(at);
    this.phase = 'silence';
    return out;
  }

  private close(at: number): SegmentEvent[] {
    const durationMs = at - this.utteranceStart;
    if (durationMs < this.opts.minUtteranceMs) return [{ type: 'dropped', at, durationMs }];
    return [{ type: 'speech-end', at, startedAt: this.utteranceStart, durationMs }];
  }
}

/** Root-mean-square of a time-domain analyser frame (Uint8, centred on 128) as 0..1. */
export function rmsOf(frame: Uint8Array): number {
  let sum = 0;
  for (const sample of frame) {
    const v = (sample - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / Math.max(1, frame.length));
}
