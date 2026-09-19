/**
 * The quiet signals of a call (docs/stories/10_voice.md).
 *
 * A phone call tells you it is alive without saying anything: a tone when the number is taken,
 * a silence that is clearly a connected silence. This screen said nothing between "he heard me"
 * and "he answers", and a call at the ear has no screen to look at anyway (2026-09-19, "искам
 * лек ненатрапчив сигнал ... нежно бип бип"). So: two notes up when his words go off, a soft
 * pair every few seconds while Jarvis thinks, two notes down when the line drops and two up
 * when it comes back. Nothing else, and never loud.
 *
 * The notes play through the microphone's own AudioContext, so they are on the call's route and
 * inside the browser's echo canceller; and each one tells the listener to ignore the microphone
 * while it sounds, so a beep is never heard as a word.
 */

export interface Cues {
  /** His words have been handed to Jarvis. */
  accepted(): void;
  /** Jarvis is working: a pulse every few seconds while `on`. */
  thinking(on: boolean): void;
  /** The line to Jarvis is down (or back). */
  offline(on: boolean): void;
  /** The call is over. */
  close(): void;
}

/** A pair of notes: frequency in Hz, each `noteMs` long, `gapMs` apart. */
interface Pattern {
  notes: number[];
  noteMs: number;
  gapMs: number;
  gain: number;
}

const ACCEPTED: Pattern = { notes: [660, 880], noteMs: 85, gapMs: 45, gain: 0.05 };
const THINKING: Pattern = { notes: [520, 520], noteMs: 55, gapMs: 110, gain: 0.025 };
const LOST: Pattern = { notes: [440, 330], noteMs: 110, gapMs: 60, gain: 0.045 };
const BACK: Pattern = { notes: [590, 790], noteMs: 80, gapMs: 50, gain: 0.04 };

/** How often the thinking pulse repeats, and how long it waits before the first one — long
 *  enough that a quick answer is never announced, short enough that a slow one is. */
const THINKING_EVERY_MS = 2600;
const THINKING_FIRST_MS = 1400;
/** A line that stays down says so again this often; rarely, so it is a reminder, not an alarm. */
const OFFLINE_EVERY_MS = 8000;

export class ToneCues implements Cues {
  private thinkTimer: ReturnType<typeof setInterval> | null = null;
  private thinkStart: ReturnType<typeof setTimeout> | null = null;
  private offlineTimer: ReturnType<typeof setInterval> | null = null;
  private down = false;

  constructor(private readonly o: { contextOf: () => AudioContext | null; suppress?: (ms: number) => void }) {}

  accepted(): void {
    this.play(ACCEPTED);
  }

  thinking(on: boolean): void {
    if (on === (this.thinkTimer !== null || this.thinkStart !== null)) return;
    this.stopThinking();
    if (!on) return;
    this.thinkStart = setTimeout(() => {
      this.thinkStart = null;
      this.play(THINKING);
      this.thinkTimer = setInterval(() => this.play(THINKING), THINKING_EVERY_MS);
    }, THINKING_FIRST_MS);
  }

  offline(on: boolean): void {
    if (on === this.down) return;
    this.down = on;
    if (on) {
      this.play(LOST);
      this.offlineTimer = setInterval(() => this.play(LOST), OFFLINE_EVERY_MS);
      return;
    }
    if (this.offlineTimer) clearInterval(this.offlineTimer);
    this.offlineTimer = null;
    this.play(BACK);
  }

  close(): void {
    this.stopThinking();
    if (this.offlineTimer) clearInterval(this.offlineTimer);
    this.offlineTimer = null;
    this.down = false;
  }

  private stopThinking(): void {
    if (this.thinkStart) clearTimeout(this.thinkStart);
    if (this.thinkTimer) clearInterval(this.thinkTimer);
    this.thinkStart = null;
    this.thinkTimer = null;
  }

  /** One pattern, scheduled on the audio clock so the notes are even. Silent if there is no
   *  context yet, or it is not running: a cue is never worth waking anything for. */
  private play(p: Pattern): void {
    const ctx = this.o.contextOf();
    if (ctx?.state !== 'running') return;
    const fade = 0.012;
    let at = ctx.currentTime + 0.01;
    for (const hz of p.notes) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = hz;
      const dur = p.noteMs / 1000;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(p.gain, at + fade);
      gain.gain.setValueAtTime(p.gain, at + dur - fade);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(at);
      osc.stop(at + dur + 0.01);
      at += dur + p.gapMs / 1000;
    }
    // The microphone hears these too: hold it shut for as long as they sound, and a little over.
    this.o.suppress?.((at - ctx.currentTime) * 1000 + 150);
  }
}
