/**
 * The phone as a meeting recorder (2026-10-14: "стартирам среща от телефона и дори да заключа
 * телефона да продължава до момента, в който не натисна стоп").
 *
 * The microphone goes through an AudioWorklet (the same tap a call uses), down to 16 kHz mono,
 * and is cut into pieces of 30-45 s at a pause - never in the middle of a word, so each piece is
 * recognised whole. A piece is written to IndexedDB first and uploaded after, in order, until the
 * core has acknowledged it; it is numbered, so a retry after a lost answer is not heard twice.
 * Nothing depends on timers to keep recording: the audio thread drives everything, which is what
 * keeps working on Android with the screen locked (Chrome keeps a page that is capturing the
 * microphone alive and shows it in the status bar). A notification with Stop stands in for the
 * screen while it is locked.
 *
 * If Android kills the page anyway, what was recorded is on the phone: the next time the app is
 * opened the pieces are uploaded and the meeting can be continued or finished. The piece still
 * being recorded is saved as a draft every 10 s, so a kill loses at most those seconds (the
 * alpha test lost the whole open piece, half a minute, before the drafts).
 */

import { api } from '../api/client';
import { wavBlob } from '../voice/listener';

const RATE = 16_000;
const FRAME = RATE / 10; // 100 ms, the unit the cut is decided in
const MIN_PIECE_S = 30;
const MAX_PIECE_S = 45;
const SAVED_KEY = 'jarvis.meeting.recording';
const DB_NAME = 'jarvis-meetings';
const STORE = 'pieces';
const NOTIFICATION_TAG = 'jarvis-meeting';
const DRAFT_EVERY_S = 10;

interface Piece {
  key: string;
  meetingId: string;
  n: number;
  t0: number;
  blob: Blob;
  /** The piece still being recorded, kept against a kill; never uploaded as it is. */
  draft?: boolean;
}

const draftKey = (meetingId: string) => `draft:${meetingId}`;

interface Saved {
  meetingId: string;
  title: string;
  startedAt: number;
  nextN: number;
  /** Stop was asked for: once every piece is up, the core is told. */
  finishing: boolean;
}

export interface RecorderState {
  meetingId: string | null;
  title: string;
  /** The microphone is open and recording on this device. */
  recording: boolean;
  /** A recording of this device that is not running: the page was closed or killed meanwhile. */
  interrupted: boolean;
  startedAt: number;
  /** Pieces recorded here and not yet acknowledged by the core. */
  pending: number;
  /** Stop was asked for and the last pieces are still going up. */
  finishing: boolean;
  problem: string | null;
}

// --- IndexedDB, the phone's own copy of every piece until the core has it ----------------------

function db(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: 'key' });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB unavailable'));
  });
}

async function tx<T>(mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const d = await db();
  return new Promise<T>((resolve, reject) => {
    const req = run(d.transaction(STORE, mode).objectStore(STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB error'));
  });
}

const putPiece = (p: Piece) => tx('readwrite', (s) => s.put(p));
const deletePiece = (key: string) => tx('readwrite', (s) => s.delete(key));
async function allPieces(drafts = false): Promise<Piece[]> {
  const all = ((await tx('readonly', (s) => s.getAll())) as Piece[]).filter((p) => drafts || !p.draft);
  return all.sort((a, b) => (a.meetingId === b.meetingId ? a.n - b.n : a.meetingId.localeCompare(b.meetingId)));
}

function readSaved(): Saved | null {
  try {
    const raw = localStorage.getItem(SAVED_KEY);
    return raw ? (JSON.parse(raw) as Saved) : null;
  } catch {
    return null;
  }
}

function writeSaved(s: Saved | null): void {
  try {
    if (s) localStorage.setItem(SAVED_KEY, JSON.stringify(s));
    else localStorage.removeItem(SAVED_KEY);
  } catch {
    /* private mode: the pieces in IndexedDB are what matters */
  }
}

/** Where to cut a buffer of 100 ms frame levels: at the quietest frame between MIN and MAX. */
export function quietestCut(levels: number[], minFrames: number, maxFrames: number): number {
  let best = Math.min(levels.length, maxFrames);
  let low = Infinity;
  for (let i = minFrames; i < Math.min(levels.length, maxFrames); i += 1) {
    const v = levels[i] ?? Infinity;
    if (v < low) {
      low = v;
      best = i;
    }
  }
  return best;
}

// --- the recorder ----------------------------------------------------------------------------

export class PhoneRecorder {
  private state: RecorderState = {
    meetingId: null,
    title: '',
    recording: false,
    interrupted: false,
    startedAt: 0,
    pending: 0,
    finishing: false,
    problem: null,
  };
  private listeners = new Set<(s: RecorderState) => void>();
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private tap: AudioWorkletNode | null = null;
  /** Samples of the piece being recorded, and their 100 ms levels. */
  private frames: Int16Array[] = [];
  private buffered = 0;
  private levels: number[] = [];
  private levelAcc = 0;
  private levelCount = 0;
  /** Seconds from the meeting's start at which the piece being recorded began. */
  private pieceT0 = 0;
  private saved: Saved | null = null;
  private pumping = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryMs = 2000;

  constructor() {
    if (typeof window === 'undefined') return;
    window.addEventListener('online', () => void this.pump());
    navigator.serviceWorker?.addEventListener('message', (e: MessageEvent<{ type?: string; id?: string }>) => {
      if (e.data?.type === 'jarvis.meeting.stop' && e.data.id === this.state.meetingId) void this.stop();
    });
  }

  get snapshot(): RecorderState {
    return this.state;
  }

  subscribe(fn: (s: RecorderState) => void): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }

  private set(patch: Partial<RecorderState>): void {
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn(this.state);
  }

  /** On app start: a recording this phone did not finish (the page was killed), and pieces that
   *  are still waiting to go up. */
  async recover(): Promise<void> {
    if (this.state.recording) return;
    const saved = readSaved();
    let pending = 0;
    try {
      pending = (await allPieces()).length;
    } catch {
      /* no IndexedDB: nothing was kept */
    }
    if (saved) {
      this.saved = saved;
      pending += await this.promoteDraft(saved);
      this.set({ meetingId: saved.meetingId, title: saved.title, startedAt: saved.startedAt, interrupted: !saved.finishing, finishing: saved.finishing, pending });
    } else {
      this.set({ pending });
    }
    void this.pump();
  }

  /** A draft left by a page that was killed becomes the next piece. */
  private async promoteDraft(saved: Saved): Promise<number> {
    try {
      const draft = (await allPieces(true)).find((p) => p.key === draftKey(saved.meetingId));
      if (!draft) return 0;
      const n = saved.nextN;
      saved.nextN += 1;
      writeSaved(saved);
      await putPiece({ ...draft, key: `${saved.meetingId}:${String(n).padStart(6, '0')}`, n, draft: false });
      await deletePiece(draft.key);
      return 1;
    } catch {
      return 0;
    }
  }

  /** Start recording a meeting the core has just created (host "phone"). From the tap. */
  async start(meeting: { id: string; title: string }, resume = false): Promise<void> {
    if (this.state.recording) throw new Error('This phone is already recording a meeting.');
    const prior = resume && this.saved?.meetingId === meeting.id ? this.saved : null;
    const startedAt = prior?.startedAt ?? Date.now();
    this.saved = { meetingId: meeting.id, title: meeting.title, startedAt, nextN: prior?.nextN ?? 0, finishing: false };
    writeSaved(this.saved);
    try {
      this.ctx = new AudioContext();
      if (this.ctx.state === 'suspended') await this.ctx.resume();
      await this.ctx.audioWorklet.addModule('/pcm-worklet.js');
      // A room, not a call: no echo cancelling (nothing is played back), the rest on.
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      this.release();
      writeSaved(prior ?? null);
      throw new Error(e instanceof Error ? `The microphone could not be opened: ${e.message}` : 'The microphone could not be opened.');
    }
    this.tap = new AudioWorkletNode(this.ctx, 'pcm-tap', { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1 });
    const rate = this.ctx.sampleRate;
    this.tap.port.onmessage = (e: MessageEvent<Float32Array>) => this.onAudio(e.data, rate);
    this.ctx.createMediaStreamSource(this.stream).connect(this.tap);
    // Times are from the meeting's start: after a resume the gap the page was dead stays a gap.
    this.pieceT0 = (Date.now() - startedAt) / 1000;
    this.frames = [];
    this.buffered = 0;
    this.levels = [];
    this.set({ meetingId: meeting.id, title: meeting.title, recording: true, interrupted: false, startedAt, finishing: false, problem: null });
    void this.notify(meeting);
  }

  /** Stop: the last piece is cut, everything still on the phone goes up, then the core is told
   *  (and summarises). Without a network the stop waits on the phone and finishes later. */
  async stop(): Promise<void> {
    const saved = this.saved;
    if (!saved) return;
    if (this.state.recording) {
      this.cut(this.levels.length, true);
      this.release();
    }
    saved.finishing = true;
    writeSaved(saved);
    this.set({ recording: false, interrupted: false, finishing: true });
    void this.closeNotification();
    await this.pump();
    await this.finishIfDrained();
  }

  /** The page was killed: finish without the rest (what was recorded is summarised). */
  async finishInterrupted(): Promise<void> {
    await this.stop();
  }

  private async finishIfDrained(): Promise<void> {
    const saved = this.saved;
    if (!saved?.finishing) return;
    const left = (await allPieces()).filter((p) => p.meetingId === saved.meetingId).length;
    if (left > 0) return; // pump calls back when they are up
    try {
      await api.meetings.stop(saved.meetingId);
    } catch (e) {
      this.set({ problem: `Stop did not reach Jarvis: ${e instanceof Error ? e.message : 'error'}; it will be retried.` });
      this.scheduleRetry();
      return;
    }
    this.saved = null;
    writeSaved(null);
    this.set({ meetingId: null, title: '', finishing: false, interrupted: false, problem: null });
  }

  private release(): void {
    this.tap?.port.close();
    this.tap?.disconnect();
    this.tap = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    void this.ctx?.close();
    this.ctx = null;
  }

  private onAudio(input: Float32Array, rate: number): void {
    const step = rate / RATE;
    const n = Math.floor(input.length / step);
    const out = new Int16Array(n);
    for (let i = 0; i < n; i += 1) {
      const v = Math.max(-1, Math.min(1, input[Math.floor(i * step)] ?? 0));
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      this.levelAcc += v * v;
      this.levelCount += 1;
      if (this.levelCount === FRAME) {
        this.levels.push(Math.sqrt(this.levelAcc / FRAME));
        this.levelAcc = 0;
        this.levelCount = 0;
      }
    }
    this.frames.push(out);
    const before = this.buffered;
    this.buffered += n;
    const seconds = this.buffered / RATE;
    if (Math.floor(seconds / DRAFT_EVERY_S) > Math.floor(before / RATE / DRAFT_EVERY_S)) this.saveDraft();
    if (seconds < MIN_PIECE_S) return;
    const minFrames = MIN_PIECE_S * 10;
    const last = this.levels.at(-1) ?? 1;
    const floor = Math.min(...this.levels.slice(-150));
    // At a pause once it is long enough; at the quietest moment when it gets too long.
    if (last <= Math.max(floor * 1.5, 0.004)) this.cut(this.levels.length, false);
    else if (seconds >= MAX_PIECE_S) this.cut(quietestCut(this.levels, minFrames, MAX_PIECE_S * 10), false);
  }

  /** The open piece as it is now, against the page being killed before it is cut. */
  private saveDraft(): void {
    const saved = this.saved;
    if (!saved) return;
    const all = new Int16Array(this.buffered);
    let o = 0;
    for (const f of this.frames) {
      all.set(f, o);
      o += f.length;
    }
    const draft: Piece = { key: draftKey(saved.meetingId), meetingId: saved.meetingId, n: -1, t0: this.pieceT0, blob: wavBlob(all, RATE), draft: true };
    void putPiece(draft).catch(() => undefined);
  }

  /** Cut the piece being recorded at a frame boundary; the rest starts the next piece. */
  private cut(atFrame: number, last: boolean): void {
    const saved = this.saved;
    if (!saved) return;
    const all = new Int16Array(this.buffered);
    let o = 0;
    for (const f of this.frames) {
      all.set(f, o);
      o += f.length;
    }
    const end = last ? all.length : Math.min(all.length, atFrame * FRAME);
    const piece = all.subarray(0, end);
    const rest = all.slice(end);
    const t0 = this.pieceT0;
    this.pieceT0 += end / RATE;
    this.frames = rest.length ? [rest] : [];
    this.buffered = rest.length;
    this.levels = this.levels.slice(Math.ceil(end / FRAME));
    void deletePiece(draftKey(saved.meetingId)).catch(() => undefined);
    if (piece.length < RATE / 2) return; // under half a second: nothing to hear
    const n = saved.nextN;
    saved.nextN += 1;
    writeSaved(saved);
    const p: Piece = { key: `${saved.meetingId}:${String(n).padStart(6, '0')}`, meetingId: saved.meetingId, n, t0, blob: wavBlob(piece, RATE) };
    void putPiece(p)
      .catch(() => undefined)
      .then(() => this.pump());
  }

  /** Upload what is on the phone, oldest first, one at a time, until it is all acknowledged. */
  private async pump(): Promise<void> {
    if (this.pumping) return;
    this.pumping = true;
    try {
      for (;;) {
        let pieces: Piece[];
        try {
          pieces = await allPieces();
        } catch {
          return;
        }
        this.set({ pending: pieces.length });
        const next = pieces[0];
        if (!next) break;
        try {
          await api.meetings.chunk(next.meetingId, next.blob, next.t0, next.n);
        } catch (e) {
          const status = (e as { status?: number }).status;
          if (status === 404) {
            await deletePiece(next.key); // the meeting is gone: nothing to deliver it to
            continue;
          }
          this.set({ problem: `Waiting to upload ${pieces.length} piece(s): ${e instanceof Error ? e.message : 'no connection'}.` });
          this.scheduleRetry();
          return;
        }
        await deletePiece(next.key);
        this.retryMs = 2000;
        if (this.state.problem?.startsWith('Waiting to upload')) this.set({ problem: null });
      }
    } finally {
      this.pumping = false;
    }
    await this.finishIfDrained();
  }

  private scheduleRetry(): void {
    if (this.retryTimer) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.pump();
    }, this.retryMs);
    this.retryMs = Math.min(60_000, this.retryMs * 2);
  }

  // --- the notification that stands in for the locked screen ---------------------------------

  private async notify(meeting: { id: string; title: string }): Promise<void> {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      const reg = await navigator.serviceWorker?.ready;
      await reg?.showNotification(`Recording: ${meeting.title}`, {
        tag: NOTIFICATION_TAG,
        body: 'Jarvis is listening. Stop when the meeting is over.',
        requireInteraction: true,
        silent: true,
        data: { type: 'meeting', id: meeting.id, path: '/meetings' },
        actions: [{ action: 'meeting-stop', title: 'Stop' }],
      } as NotificationOptions);
    } catch {
      /* the app's own Stop is always there */
    }
  }

  private async closeNotification(): Promise<void> {
    try {
      const reg = await navigator.serviceWorker?.ready;
      for (const n of (await reg?.getNotifications({ tag: NOTIFICATION_TAG })) ?? []) n.close();
    } catch {
      /* nothing to close */
    }
  }
}

export const phoneRecorder = new PhoneRecorder();
