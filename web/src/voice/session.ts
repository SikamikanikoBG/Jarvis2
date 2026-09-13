/**
 * The call: one state machine, driven by the listener (what Arsen says), the speaker (what
 * Jarvis says out loud) and the run events the store already receives. No React, no DOM: the
 * listener and the speaker are interfaces, so the whole thing runs under vitest with fakes, and
 * the call screen is a pure function of `state`.
 *
 *   idle → connecting → listening ⇄ transcribing → thinking → speaking → listening …
 *
 * Cut-in: while `speaking`, speech from the listener stops the speaker on the spot. The words
 * then reach the run that is still working as a steer, or start a new run — "too late" is what
 * the core turns into a new voice turn, so the session never has to guess.
 */

import { SentenceSplitter, scriptLanguage, speakable } from './sentences';

export type CallPhase = 'idle' | 'connecting' | 'listening' | 'transcribing' | 'thinking' | 'speaking' | 'ended';

export interface CallState {
  phase: CallPhase;
  /** What Arsen last said, as recognised. */
  heard: string;
  /** What Jarvis is saying, sentence by sentence; `spokenUpTo` is how many have been said. */
  saying: string[];
  spokenUpTo: number;
  /** The run being answered, if any. */
  runId: string | null;
  /** The last thing that went wrong, said once, in a sentence a person can act on. */
  problem: string | null;
  /** Live level from the microphone (0..1), for the ring. */
  level: number;
  muted: boolean;
  startedAt: number;
}

export interface Listener {
  start(handlers: ListenerHandlers): Promise<void>;
  stop(): void;
  /** While Jarvis speaks the listener is gated: only a clear cut-in gets through. */
  setGated(gated: boolean): void;
  setMuted(muted: boolean): void;
}

export interface ListenerHandlers {
  onLevel(level: number): void;
  onSpeechStart(): void;
  /** An utterance has ended and its audio is on its way to be recognised. */
  onUtterance(audio: Blob): void;
  onError(message: string): void;
}

export interface Speaker {
  /** Say one sentence; resolves when it has been said (or was cut off). */
  speak(text: string, lang: string): Promise<void>;
  /** Stop mid-sentence and drop what was queued. */
  cancel(): void;
  /** Whether this device can speak at all (and in what). */
  available(): boolean;
}

export interface Transcriber {
  transcribe(audio: Blob): Promise<{ text: string; language: string | null }>;
}

export interface Transport {
  /** Start a voice run; the run id arrives later through `onRunQueued`. */
  create(text: string, conversationId: string | null): boolean;
  /** Hand a working run more words. False = the socket is down, not "too late". */
  steer(runId: string, text: string): boolean;
  cancel(runId: string): void;
}

export interface SessionOptions {
  listener: Listener;
  speaker: Speaker;
  transcriber: Transcriber;
  transport: Transport;
  /** The language the settings prefer when the script does not decide (usually "bg"). */
  language: string;
  conversationId: string | null;
  onChange(state: CallState): void;
  now?: () => number;
}

const INITIAL: CallState = {
  phase: 'idle',
  heard: '',
  saying: [],
  spokenUpTo: 0,
  runId: null,
  problem: null,
  level: 0,
  muted: false,
  startedAt: 0,
};

export class CallSession {
  state: CallState = { ...INITIAL };
  private splitter = new SentenceSplitter();
  private queue: string[] = [];
  private speaking: Promise<void> | null = null;
  private cutIn = false;
  private conversationId: string | null;
  private readonly now: () => number;
  /** Utterances recognised while a run was still being created: one voice turn, not two. */
  private pendingCreate = false;

  constructor(private readonly o: SessionOptions) {
    this.conversationId = o.conversationId;
    this.now = o.now ?? (() => Date.now());
  }

  // --- lifecycle --------------------------------------------------------------------------

  async start(): Promise<void> {
    this.set({ ...INITIAL, phase: 'connecting', startedAt: this.now() });
    if (!this.o.speaker.available()) {
      this.set({ problem: 'This device has no voice to speak with.' });
    }
    try {
      await this.o.listener.start({
        onLevel: (level) => this.set({ level }),
        onSpeechStart: () => this.onSpeechStart(),
        onUtterance: (audio) => void this.onUtterance(audio),
        onError: (message) => this.fail(message),
      });
    } catch (e) {
      this.fail(e instanceof Error ? e.message : 'The microphone could not be opened.');
      this.set({ phase: 'ended' });
      return;
    }
    this.set({ phase: 'listening' });
  }

  end(): void {
    this.o.listener.stop();
    this.o.speaker.cancel();
    // A run still working is left to finish: its words land in the chat, only the reading
    // aloud stops. Cancelling it would throw away an answer that was already being written.
    this.queue = [];
    this.set({ phase: 'ended', level: 0 });
  }

  setMuted(muted: boolean): void {
    this.o.listener.setMuted(muted);
    this.set({ muted });
  }

  // --- from the listener -------------------------------------------------------------------

  private onSpeechStart(): void {
    if (this.state.phase === 'speaking') {
      // Cut-in: he stops mid-sentence and listens. What was half-written before the cut is
      // dropped too — it was an answer to the question he has just been talked out of.
      this.cutIn = true;
      this.o.speaker.cancel();
      this.queue = [];
      this.splitter = new SentenceSplitter();
      this.set({ phase: 'listening' });
    }
  }

  private async onUtterance(audio: Blob): Promise<void> {
    if (this.state.phase === 'ended' || this.state.muted) return;
    this.set({ phase: 'transcribing', problem: null });
    let text = '';
    try {
      const res = await this.o.transcriber.transcribe(audio);
      text = res.text.trim();
    } catch (e) {
      this.fail(`I can't hear you right now — the speech service is not answering (${e instanceof Error ? e.message : 'error'}).`);
      this.set({ phase: 'listening' });
      return;
    }
    if (!text) {
      this.set({ phase: 'listening' });
      return;
    }
    this.set({ heard: text, phase: 'thinking', saying: [], spokenUpTo: 0 });
    // A run still working gets the words; otherwise a new voice turn. If the run finished
    // between the two, the core turns the steer into a new turn itself.
    const sent = this.state.runId && !this.pendingCreate ? this.o.transport.steer(this.state.runId, text) : this.create(text);
    if (!sent) {
      this.fail('The connection to Jarvis is down; try again in a moment.');
      this.set({ phase: 'listening' });
    }
  }

  private create(text: string): boolean {
    this.pendingCreate = true;
    this.splitter = new SentenceSplitter();
    return this.o.transport.create(text, this.conversationId);
  }

  // --- from the run events (the store forwards these) ---------------------------------------

  onRunQueued(runId: string, conversationId: string): void {
    if (this.state.phase === 'ended') return;
    // Ours if we asked for it, or if it is a new run in our conversation while we hold none — a
    // late cut-in the core turned into a run of its own. A run in another chat is not ours.
    const ours = this.pendingCreate || (conversationId === this.conversationId && this.state.runId === null);
    if (!ours) return;
    this.pendingCreate = false;
    this.conversationId = conversationId;
    this.splitter = new SentenceSplitter();
    this.set({ runId });
  }

  onDelta(runId: string, text: string): void {
    if (runId !== this.state.runId || this.state.phase === 'ended') return;
    for (const sentence of this.splitter.push(text)) this.enqueue(sentence);
  }

  onRunDone(runId: string): void {
    if (runId !== this.state.runId) return;
    const rest = this.splitter.flush();
    if (rest) this.enqueue(rest);
    this.set({ runId: null });
    if (this.queue.length === 0 && !this.speaking) this.backToListening();
  }

  onRunFailed(runId: string, error: string): void {
    if (runId !== this.state.runId) return;
    this.set({ runId: null });
    this.o.speaker.cancel();
    this.queue = [];
    this.fail(`Something went wrong on my side: ${error}`);
    void this.sayProblem();
  }

  // --- speaking ---------------------------------------------------------------------------

  private enqueue(sentence: string): void {
    const clean = speakable(sentence);
    if (!clean) return;
    this.queue.push(clean);
    this.set({ saying: [...this.state.saying, clean] });
    this.speaking ??= this.drain();
  }

  private async drain(): Promise<void> {
    this.cutIn = false;
    this.o.listener.setGated(true);
    this.set({ phase: 'speaking' });
    try {
      while (this.queue.length > 0 && !this.cutIn && this.state.phase !== 'ended') {
        const sentence = this.queue.shift();
        if (sentence === undefined) break;
        await this.o.speaker.speak(sentence, scriptLanguage(sentence, this.o.language));
        if (!this.cutIn) this.set({ spokenUpTo: this.state.spokenUpTo + 1 });
      }
    } finally {
      this.speaking = null;
      this.o.listener.setGated(false);
    }
    if (this.state.phase === 'ended') return;
    if (this.cutIn) return; // already listening; the cut-in's words are on their way
    if (this.state.runId) this.set({ phase: 'thinking' }); // more is being written
    else this.backToListening();
  }

  private backToListening(): void {
    if (this.state.phase !== 'ended') this.set({ phase: 'listening' });
  }

  private async sayProblem(): Promise<void> {
    const p = this.state.problem;
    if (!p || !this.o.speaker.available()) {
      this.backToListening();
      return;
    }
    this.set({ phase: 'speaking' });
    try {
      await this.o.speaker.speak(p, scriptLanguage(p, this.o.language));
    } finally {
      this.backToListening();
    }
  }

  private fail(message: string): void {
    this.set({ problem: message });
  }

  private set(patch: Partial<CallState>): void {
    this.state = { ...this.state, ...patch };
    this.o.onChange(this.state);
  }
}
