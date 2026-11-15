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
  /** Silence must hold this long before the utterance is closed (a pause for breath is not the end).
   *  500 ms since 2026-09-20: it was 750, and the quarter second was pure waiting on every turn.
   *  A sentence he carries on with after the close is no longer a lost turn — the words that
   *  follow join the run as a steer, which the core was taught to take even on its last step. */
  releaseMs: number;
  /** Silence this long is reported as a `pause` — maybe the end, maybe a breath — so the words
   *  so far can be recognised while the release is still being waited out. */
  pauseMs: number;
  /** Utterances shorter than this are dropped (a cough is not a sentence). */
  minUtteranceMs: number;
  /** How far back an utterance may start before its confirmed attack: to the first sound of it,
   *  when the attack began again on dips inside the words (see `onset`). Over Jarvis's voice or
   *  the hold music the attack is 450 ms, and a cut-in took 2.7 s to confirm (alpha, 2026-11-15). */
  onsetReachMs: number;
  /** How long a sound that has not become speech is reported as one (`onset` until `quiet`): a
   *  turn waits on it no longer than this. A café's clatter never becomes speech. */
  onsetHoldMs: number;
}

export const DEFAULT_SEGMENTER: SegmenterOptions = {
  ratio: 2.5,
  minLevel: 0.012,
  attackMs: 200,
  warmupMs: 600,
  releaseMs: 500,
  pauseMs: 250,
  minUtteranceMs: 400,
  onsetReachMs: 3000,
  onsetHoldMs: 1500,
};

export type SegmentEvent =
  /** Sound over the floor, not yet held long enough to be speech: maybe a click, maybe the first
   *  syllable of the next sentence. A `speech-start` or a `quiet` follows. */
  | { type: 'onset'; at: number }
  /** The sound since the last `onset` came to nothing: silent for a release since `at`, or still
   *  not speech after `onsetHoldMs`. */
  | { type: 'quiet'; at: number }
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
  /** The first sound of what may become an utterance (an `onset` was reported), and the last
   *  loud frame of it. An attack that a dip inside a word restarted is still that sound: the
   *  utterance starts at it, not at the restart (2026-11-14, alpha: "Чакай" and the first words
   *  after a send were cut off - the 450 ms cut-in attack restarted at every gap). */
  private soundSince: number | null = null;
  private lastLoud = 0;
  /** An `onset` was reported and no `quiet` yet. A sound that went on for onsetHoldMs without
   *  becoming speech gets its `quiet` early, and no new onset until the room has been quiet once,
   *  so noise cannot hold his turn open - but it is still where an utterance would start. */
  private onsetOpen = false;
  readonly opts: SegmenterOptions;

  constructor(opts: Partial<SegmenterOptions> = {}) {
    this.opts = { ...DEFAULT_SEGMENTER, ...opts };
    this.floor = this.opts.minLevel;
    this.floorAtStart = this.floor;
  }

  get speaking(): boolean {
    return this.phase === 'speech' || this.phase === 'release';
  }

  /** Sound right now, not a silence that may yet be a pause: where a long stretch may be cut. */
  get talking(): boolean {
    return this.phase === 'speech';
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

    if (loud && (this.phase === 'silence' || this.phase === 'attack')) this.lastLoud = t;
    // A sound that has not become speech in all that time is most likely the room (a café's
    // clatter), and a turn must not wait on it any longer.
    if (this.onsetOpen && this.soundSince !== null && t - this.soundSince >= o.onsetHoldMs) {
      this.onsetOpen = false;
      out.push({ type: 'quiet', at: t });
    }
    switch (this.phase) {
      case 'silence':
        if (loud) {
          this.phase = 'attack';
          this.phaseSince = t;
          this.floorAtStart = this.floor;
          if (this.soundSince === null) {
            this.soundSince = t;
            this.onsetOpen = true;
            out.push({ type: 'onset', at: t });
          }
        } else if (this.soundSince !== null && t - this.lastLoud >= o.releaseMs) {
          // Quiet for as long as an utterance takes to close: whatever it was, it was not words.
          this.soundSince = null;
          if (this.onsetOpen) out.push({ type: 'quiet', at: this.lastLoud });
          this.onsetOpen = false;
        }
        break;
      case 'attack':
        if (!loud) {
          this.phase = 'silence';
        } else if (t - this.phaseSince >= (this.attackOverride ?? o.attackMs)) {
          this.phase = 'speech';
          const since = this.soundSince ?? this.phaseSince;
          this.utteranceStart = this.phaseSince - since <= o.onsetReachMs ? since : this.phaseSince;
          this.soundSince = null;
          this.onsetOpen = false;
          out.push({ type: 'speech-start', at: this.utteranceStart });
        }
        break;
      case 'speech':
        // No ceiling on how long he talks (2026-11-14): a 30 s one closed a briefing mid-sentence,
        // and every half minute of it became a turn of its own. The listener cuts a long stretch
        // into pieces for Whisper instead (listener.ts, PIECE_MAX_MS); the utterance goes on.
        if (!loud) {
          this.phase = 'release';
          this.phaseSince = t;
          this.pauseSent = false;
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

  /**
   * Close the utterance at the pause `at` now, before the release has run out: the turn model is
   * sure he has finished. Only while that same pause is still going on — if he has spoken since,
   * the pause was a breath and nothing closes. Answers the events, like `push`.
   */
  endAt(at: number): SegmentEvent[] {
    if (this.phase !== 'release' || this.phaseSince !== at) return [];
    const out = this.close(at);
    this.phase = 'silence';
    return out;
  }

  /** The stream stopped (the call ended, the mic was lost): close whatever is open. */
  end(t: number): SegmentEvent[] {
    if (!this.speaking) {
      this.phase = 'silence';
      // A sound that never became speech is over too: nothing may wait on it.
      const open = this.onsetOpen;
      this.soundSince = null;
      this.onsetOpen = false;
      return open ? [{ type: 'quiet', at: this.lastLoud }] : [];
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
