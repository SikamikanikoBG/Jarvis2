/**
 * Hold music: something soft to hear while Jarvis thinks, instead of silence (2026-10-13,
 * "асансьорна музичка докато мисли").
 *
 * Made here, not played from a file: a slow lo-fi loop — electric-piano chords, a quiet bass and a
 * sparse arpeggio — synthesised on the call's own AudioContext, so it is on the call's route and
 * inside the browser's echo canceller like his voice. Four bars of a progression, then another
 * progression; the arpeggio is a little different every time, so it never sounds like a tape.
 *
 * Notes are scheduled on the audio clock a little ahead of time (a look-ahead timer), so the
 * rhythm stays even even when the page is busy. It fades in, and fades out in a fraction of a
 * second when he starts to speak.
 */

const BPM = 72;
const BEAT = 60 / BPM;
const BAR = 4 * BEAT;
/** How far ahead notes are put on the audio clock, and how often the scheduler looks. */
const AHEAD_S = 0.8;
const TICK_MS = 150;
const FADE_IN_S = 1.6;
const FADE_OUT_S = 0.35;

/** MIDI note numbers. Two progressions in C, four bars each. */
const PROGRESSIONS: number[][][] = [
  // Fmaj7 – Em7 – Dm7 – Cmaj7
  [
    [53, 57, 60, 64],
    [52, 55, 59, 62],
    [50, 53, 57, 60],
    [48, 52, 55, 59],
  ],
  // Am7 – D9 – Gmaj7 – Cmaj7
  [
    [57, 60, 64, 67],
    [50, 54, 57, 64],
    [55, 59, 62, 66],
    [48, 52, 55, 59],
  ],
];

const hz = (midi: number) => 440 * 2 ** ((midi - 69) / 12);

export interface HoldMusicOptions {
  /** 0..1, how loud; 0.4 sits under a voice the way a phone's hold music does. */
  volume: number;
  /** The randomness of the arpeggio (tests pass a fixed one). */
  random?: () => number;
}

export class HoldMusic {
  private master: GainNode | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private nextBar = 0;
  private bar = 0;
  private progression = 0;
  private readonly random: () => number;

  constructor(
    private readonly ctx: AudioContext,
    private readonly o: HoldMusicOptions,
  ) {
    this.random = o.random ?? Math.random;
  }

  get playing(): boolean {
    return this.timer !== null;
  }

  start(): void {
    if (this.timer) return;
    const ctx = this.ctx;
    const now = ctx.currentTime;
    const tone = ctx.createBiquadFilter();
    tone.type = 'lowpass';
    tone.frequency.value = 2200;
    const master = ctx.createGain();
    master.gain.setValueAtTime(0.0001, now);
    master.gain.exponentialRampToValueAtTime(this.level(), now + FADE_IN_S);
    master.connect(tone).connect(ctx.destination);
    this.master = master;
    this.nextBar = now + 0.05;
    this.bar = 0;
    this.progression = Math.floor(this.random() * PROGRESSIONS.length);
    this.schedule();
    this.timer = setInterval(() => this.schedule(), TICK_MS);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    const master = this.master;
    this.master = null;
    if (!master) return;
    const now = this.ctx.currentTime;
    master.gain.cancelScheduledValues(now);
    master.gain.setValueAtTime(Math.max(master.gain.value, 0.0001), now);
    master.gain.exponentialRampToValueAtTime(0.0001, now + FADE_OUT_S);
    // Notes already scheduled play into a silent gain; the node goes once they are over.
    setTimeout(() => master.disconnect(), (FADE_OUT_S + AHEAD_S + 2) * 1000);
  }

  /** Master gain. At the ear the music goes to the earpiece, which is far quieter than the
   *  loudspeaker: at 0.16 x volume it was ~13 dB under his voice and vanished there (2026-10-14).
   *  0.5 x volume puts the default ~5 dB under the voice. */
  private level(): number {
    return Math.max(0.0001, 0.5 * Math.min(1, Math.max(0, this.o.volume)));
  }

  /** Put every bar that starts within the look-ahead on the clock. */
  private schedule(): void {
    if (!this.master) return;
    while (this.nextBar < this.ctx.currentTime + AHEAD_S) {
      this.playBar(this.nextBar);
      this.nextBar += BAR;
      this.bar += 1;
      if (this.bar % 4 === 0) this.progression = (this.progression + 1) % PROGRESSIONS.length;
    }
  }

  private playBar(at: number): void {
    const prog = PROGRESSIONS[this.progression] ?? PROGRESSIONS[0] ?? [];
    const chord = prog[this.bar % 4] ?? [];
    if (!chord.length) return;
    // The chord, held for the bar, softly — and once more, quieter, on beat three.
    for (const note of chord) {
      this.keys(hz(note), at, BAR * 0.95, 0.09);
      this.keys(hz(note), at + 2 * BEAT, BAR * 0.45, 0.045);
    }
    // The bass: the root an octave down, on one and three.
    const root = chord[0] ?? 48;
    this.bass(hz(root - 12), at, BEAT * 1.6);
    this.bass(hz(root - 12), at + 2 * BEAT, BEAT * 1.4);
    // A sparse arpeggio an octave up, on the off-beats; different every bar.
    for (let i = 0; i < 8; i += 1) {
      if (i % 2 === 0 || this.random() < 0.45) continue;
      const note = chord[Math.floor(this.random() * chord.length)] ?? root;
      this.keys(hz(note + 12), at + (i * BEAT) / 2, BEAT * 1.2, 0.05);
    }
  }

  /** An electric-piano-ish note: a sine with a touch of its octave, struck and left to decay. */
  private keys(freq: number, at: number, dur: number, peak: number): void {
    if (!this.master) return;
    const ctx = this.ctx;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(peak, at + 0.02);
    gain.gain.exponentialRampToValueAtTime(peak * 0.35, at + Math.min(0.6, dur * 0.4));
    gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    gain.connect(this.master);
    for (const [mult, type, share] of [
      [1, 'sine', 1],
      [2, 'triangle', 0.18],
    ] as const) {
      const osc = ctx.createOscillator();
      osc.type = type;
      osc.frequency.value = freq * mult;
      if (share === 1) {
        osc.connect(gain);
      } else {
        const g = ctx.createGain();
        g.gain.value = share;
        osc.connect(g).connect(gain);
      }
      osc.start(at);
      osc.stop(at + dur + 0.05);
    }
  }

  private bass(freq: number, at: number, dur: number): void {
    if (!this.master) return;
    const ctx = this.ctx;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.14, at + 0.03);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    const osc = ctx.createOscillator();
    osc.type = 'sine';
    osc.frequency.value = freq;
    osc.connect(gain).connect(this.master);
    osc.start(at);
    osc.stop(at + dur + 0.05);
  }
}
