/**
 * A call's data logger (settings.voice_debug): every channel on one clock, like a race car's.
 *
 * What happens at the ear cannot be watched from here (2026-10-18: "като сложа войс кола на ухото
 * все едно се мютва"), so the phone writes it down: the gravity it reads, every touch and its
 * size, the microphone level, the call's phases, each pause, cut and resume, what Whisper made of
 * each sound, the page going hidden, the audio stopping, and the line's own stats each second.
 * Batches go to the core (POST /api/voice/trace) every few seconds and at the end; the clips
 * themselves are kept beside them by the core. Off, every call here is a no-op.
 */
import { api } from '../api/client';

export type TraceEvent = { t: number; k: string } & Record<string, unknown>;

const FLUSH_MS = 3000;
/** The line's stats are read this often. */
const STATS_MS = 1000;

export class CallTrace {
  private id: string | null = null;
  private t0 = 0;
  private buf: TraceEvent[] = [];
  private last = new Map<string, number>();
  private timers: ReturnType<typeof setInterval>[] = [];
  private stats: (() => Promise<Record<string, unknown> | null>) | null = null;

  constructor(
    private readonly send: (id: string, events: TraceEvent[]) => Promise<unknown> = (id, events) => api.voice.trace(id, events),
    private readonly now: () => number = () => performance.now(),
  ) {}

  get on(): boolean {
    return this.id !== null;
  }

  start(info: Record<string, unknown>): void {
    this.stop();
    const d = new Date();
    const pad = (n: number) => String(n).padStart(2, '0');
    this.id = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}-${Math.random().toString(36).slice(2, 6)}`;
    this.t0 = this.now();
    this.mark('start', {
      at: d.toISOString(),
      ua: typeof navigator !== 'undefined' ? navigator.userAgent : null,
      screen: typeof window !== 'undefined' ? `${window.innerWidth}x${window.innerHeight}@${window.devicePixelRatio}` : null,
      ...info,
    });
    this.timers.push(setInterval(() => this.flush(), FLUSH_MS));
    this.timers.push(setInterval(() => void this.readStats(), STATS_MS));
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.onVisibility);
      window.addEventListener('pagehide', this.onPageHide);
      window.addEventListener('online', this.onOnline);
      window.addEventListener('offline', this.onOnline);
    }
  }

  stop(): void {
    if (!this.on) return;
    this.mark('end');
    this.flush();
    this.timers.forEach(clearInterval);
    this.timers = [];
    this.stats = null;
    this.last.clear();
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.onVisibility);
      window.removeEventListener('pagehide', this.onPageHide);
      window.removeEventListener('online', this.onOnline);
      window.removeEventListener('offline', this.onOnline);
    }
    this.id = null;
  }

  /** One event, stamped in ms since the call started. */
  mark(k: string, data: Record<string, unknown> = {}): void {
    if (!this.on) return;
    this.buf.push({ ...data, t: Math.round(this.now() - this.t0), k });
  }

  /** A channel that streams (gravity, level): at most one reading per `everyMs`. */
  sample(k: string, everyMs: number, data: Record<string, unknown>): void {
    if (!this.on) return;
    const now = this.now();
    if (now - (this.last.get(k) ?? Number.NEGATIVE_INFINITY) < everyMs) return;
    this.last.set(k, now);
    this.mark(k, data);
  }

  /** Where the line's stats come from, read every second while the call lasts. */
  watchStats(read: () => Promise<Record<string, unknown> | null>): void {
    this.stats = read;
  }

  private async readStats(): Promise<void> {
    if (!this.stats) return;
    try {
      const s = await this.stats();
      if (s) this.mark('line', s);
    } catch {
      /* a closing line has no stats */
    }
  }

  private flush(): void {
    if (!this.id || this.buf.length === 0) return;
    const events = this.buf;
    this.buf = [];
    void this.send(this.id, events).catch(() => undefined);
  }

  private readonly onVisibility = () => this.mark('visibility', { state: document.visibilityState });
  private readonly onPageHide = () => {
    this.mark('pagehide');
    this.flush();
  };
  private readonly onOnline = () => this.mark('network', { online: navigator.onLine });
}

/** The live call's logger. One call at a time, like the session itself (store.ts). */
export const callTrace = new CallTrace();
