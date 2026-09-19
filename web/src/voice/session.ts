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

/** However he pauses, what was recognised is sent no later than this after its first words. */
const JOIN_DEADLINE_MS = 1500;

/**
 * Where his voice comes out. The web cannot pick the earpiece or the speaker by name; what it
 * can do is hold or release the microphone. On Android, an open microphone puts Chrome in the
 * phone's call mode — audio to the earpiece; a released one leaves it — audio to the speaker.
 * "earpiece" holds the microphone throughout (so a cut-in by voice works, through the echo
 * gate); "speaker" lets go of it while he speaks and takes it back when he stops, which is
 * also the only route where a loudspeaker and a microphone cannot feed each other at all.
 */
export type CallRoute = 'earpiece' | 'speaker';

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
  route: CallRoute;
  startedAt: number;
}

export interface Listener {
  start(handlers: ListenerHandlers): Promise<void>;
  stop(): void;
  /** Jarvis started or stopped speaking: the listener gates itself (earpiece) or lets the
   *  microphone go and takes it back (speaker). */
  setSpeaking(on: boolean): void;
  setRoute(route: CallRoute): void;
  setMuted(muted: boolean): void;
}

export interface ListenerHandlers {
  onLevel(level: number): void;
  onSpeechStart(): void;
  /** Silence that may be the end: the audio so far, to be recognised now rather than after the
   *  release. `at` names the pause; an utterance that ends there carries the same `at`. */
  onPause(audio: Blob, at: number): void;
  /** An utterance has ended and its audio is on its way to be recognised. */
  onUtterance(audio: Blob, at?: number): void;
  onError(message: string): void;
}

export interface Speaker {
  /** Say one sentence; resolves when it has been said (or was cut off). */
  speak(text: string, lang: string): Promise<void>;
  /** Stop mid-sentence and drop what was queued. */
  cancel(): void;
  /** Whether this device can speak at all (and in what). */
  available(): boolean;
  /** Start getting a sentence ready before its turn (a server voice fetches it now). */
  prepare?(text: string, lang: string): void;
  /** What went wrong with the last sentence, in a sentence for the screen — and cleared by
   *  the read. A voice that falls back silently is a call that "does not work". */
  takeProblem?(): string | null;
  /** A new reply is about to be spoken: a voice that had to fall back may try its own again. */
  beginReply?(): void;
}

type Recognised = { text: string; language: string | null };

export interface Transcriber {
  /** `language` is the call's current language, a hint for the recogniser — an utterance is
   *  too short to guess a language from, and a wrong guess comes back as Greek. */
  transcribe(audio: Blob, language: string): Promise<{ text: string; language: string | null }>;
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
  /** The route the call opens on; the screen's toggle changes it live. */
  route?: CallRoute;
  /** How long a recognised utterance waits for him to go on before it is sent. A pause for
   *  breath inside a sentence closes an utterance (2026-09-16: "да може в крайна сметка" and
   *  the rest of the sentence became two turns, answered twice, spoken over each other); this
   *  is the window in which the rest joins it. Latency he pays on every turn, so short. */
  joinMs?: number;
  /** The languages the recogniser may hear (the settings' `stt_languages`); the first is the
   *  call's language until he is heard speaking another of them. */
  languages?: string[];
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
  route: 'earpiece',
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
  /** Recognised words waiting for him to finish (the join window), and the timer that sends them. */
  private held = '';
  private heldSince = 0;
  private joinTimer: ReturnType<typeof setTimeout> | null = null;
  /** When the last utterance ended: the join window counts from there, not from when the
   *  recogniser answered, so recognition time is not paid twice. */
  private quietSince = 0;
  /** A recognition started at a pause, before the utterance was known to have ended. */
  private speculative: { at: number; result: Promise<Recognised | Error> } | null = null;
  /** Words that arrived while the run was being created, steered once its id is known. */
  private steerWhenKnown = '';
  /** The language he is speaking, for the recogniser; the reply's, for the voice. Decided once
   *  per reply from its first sentence with letters, so one Latin-only sentence inside a
   *  Bulgarian answer does not switch the voice (2026-09-16, "the voices change"). */
  private heardLang: string;
  private replyLang: string | null = null;
  /** The current step's words, held until the step ends (see `onDelta`). */
  private stepText = '';

  constructor(private readonly o: SessionOptions) {
    this.conversationId = o.conversationId;
    this.now = o.now ?? (() => Date.now());
    this.heardLang = o.languages?.[0] ?? o.language;
  }

  // --- lifecycle --------------------------------------------------------------------------

  async start(): Promise<void> {
    const route = this.o.route ?? 'earpiece';
    this.set({ ...INITIAL, phase: 'connecting', startedAt: this.now(), route });
    if (!this.o.speaker.available()) {
      this.set({ problem: 'This device has no voice to speak with.' });
    }
    this.o.listener.setRoute(route);
    try {
      await this.o.listener.start({
        onLevel: (level) => this.set({ level }),
        onSpeechStart: () => this.onSpeechStart(),
        onPause: (audio, at) => this.onPause(audio, at),
        onUtterance: (audio, at) => void this.onUtterance(audio, at),
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
    this.clearJoin();
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

  setRoute(route: CallRoute): void {
    this.o.listener.setRoute(route);
    this.set({ route });
  }

  // --- from the listener -------------------------------------------------------------------

  private onSpeechStart(): void {
    if (this.state.phase === 'speaking') {
      // Cut-in: he stops mid-sentence and listens. What was half-written before the cut is
      // dropped too — it was an answer to the question he has just been talked out of.
      this.cutIn = true;
      this.o.speaker.cancel();
      this.queue = [];
      this.stepText = '';
      this.splitter = new SentenceSplitter();
      this.set({ phase: 'listening' });
    }
  }

  /**
   * A pause that may be the end: recognise what was said so far now, so that if the release
   * confirms it the words are ready when the utterance closes instead of a Whisper round-trip
   * later. If he goes on, the result is dropped unread — one wasted request, no wrong words.
   */
  private onPause(audio: Blob, at: number): void {
    if (this.state.phase === 'ended' || this.state.muted) return;
    const result = this.o.transcriber.transcribe(audio, this.heardLang).then(
      (r): Recognised | Error => r,
      (e: unknown): Recognised | Error => (e instanceof Error ? e : new Error(String(e))),
    );
    this.speculative = { at, result };
  }

  private async onUtterance(audio: Blob, at?: number): Promise<void> {
    if (this.state.phase === 'ended' || this.state.muted) return;
    this.quietSince = this.now();
    this.set({ phase: 'transcribing', problem: null });
    // The utterance ended at the pause already being recognised: that answer is this answer.
    const early = at !== undefined && this.speculative?.at === at ? this.speculative.result : null;
    this.speculative = null;
    let text = '';
    try {
      const res = early ? await early : await this.o.transcriber.transcribe(audio, this.heardLang);
      if (res instanceof Error) throw res;
      text = res.text.trim();
      // He may switch to another of the call's languages — believed only when the words are
      // written in that language's script too. Whisper labels a noise "en" as readily as
      // anything, and a hint of "en" on Bulgarian speech comes back as an English translation.
      if (res.language && this.o.languages?.includes(res.language) && text && scriptLanguage(text, res.language) === res.language) {
        this.heardLang = res.language;
      }
    } catch (e) {
      this.fail(`I can't hear you right now — the speech service is not answering (${e instanceof Error ? e.message : 'error'}).`);
      this.set({ phase: 'listening' });
      return;
    }
    if (!text) {
      this.set({ phase: this.held ? 'transcribing' : 'listening' });
      return;
    }
    const first = !this.held;
    this.held = this.held ? `${this.held} ${text}` : text;
    this.set({ heard: this.held });
    if (first) this.heldSince = this.now();
    // Sent when no more words have arrived for a moment. Only recognised words extend the wait
    // — not the microphone hearing something, or a noisy room would hold his question for ever
    // (2026-09-16: "it catches every sound but not me") — and never past the hard deadline.
    if (this.joinTimer) clearTimeout(this.joinTimer);
    const join = this.o.joinMs ?? 600;
    // The window is measured from the end of speech: the silence already spent while the
    // recogniser worked counts toward it.
    const wait = Math.max(0, Math.min(join - (this.now() - this.quietSince), this.heldSince + JOIN_DEADLINE_MS - this.now()));
    this.joinTimer = setTimeout(() => {
      this.joinTimer = null;
      this.send();
    }, wait);
  }

  private clearJoin(): void {
    if (this.joinTimer) clearTimeout(this.joinTimer);
    this.joinTimer = null;
    this.held = '';
    this.steerWhenKnown = '';
  }

  private send(): void {
    const text = this.held;
    this.held = '';
    if (!text || this.state.phase === 'ended') return;
    if (this.pendingCreate) {
      // The run we asked for has no id yet: these words follow it as a steer the moment it does.
      this.steerWhenKnown = this.steerWhenKnown ? `${this.steerWhenKnown} ${text}` : text;
      this.set({ heard: text, phase: 'thinking' });
      return;
    }
    this.set({ heard: text, phase: 'thinking', saying: [], spokenUpTo: 0 });
    // A run still working gets the words; otherwise a new voice turn. If the run finished
    // between the two, the core turns the steer into a new turn itself.
    const sent = this.state.runId ? this.o.transport.steer(this.state.runId, text) : this.create(text);
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
    this.replyLang = null;
    this.o.speaker.beginReply?.();
    this.set({ runId });
    if (this.steerWhenKnown) {
      const more = this.steerWhenKnown;
      this.steerWhenKnown = '';
      if (!this.o.transport.steer(runId, more)) this.fail('The connection to Jarvis is down; part of what you said was lost.');
    }
  }

  /**
   * A step's words are held until the step ends, and spoken only if it ended in an answer. A
   * step that ends in tool calls has written its plan ("The user is on a voice call. I need to
   * keep it short. Let me look up…") — the tool format invites reasoning before a call — and
   * that was being read out as if it were the reply (2026-09-16). Replies on a call are two or
   * three sentences, so holding them until the step is done costs a fraction of a second.
   */
  onDelta(runId: string, text: string): void {
    if (runId !== this.state.runId || this.state.phase === 'ended') return;
    this.stepText += text;
  }

  onStepDone(runId: string, endedInToolCalls: boolean): void {
    if (runId !== this.state.runId || this.state.phase === 'ended') return;
    const text = this.stepText;
    this.stepText = '';
    if (endedInToolCalls || !text) return;
    for (const sentence of this.splitter.push(text)) this.enqueue(sentence);
    const rest = this.splitter.flush();
    if (rest) this.enqueue(rest);
  }

  onRunDone(runId: string): void {
    if (runId !== this.state.runId) return;
    // A run that ends without a step-done for its last words (a cancel) still says them.
    if (this.stepText) {
      const text = this.stepText;
      this.stepText = '';
      for (const sentence of this.splitter.push(text)) this.enqueue(sentence);
    }
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
    this.o.speaker.prepare?.(clean, this.langFor(clean));
    this.set({ saying: [...this.state.saying, clean] });
    this.speaking ??= this.drain();
  }

  /** The reply's language: decided by its first sentence that has letters, then kept. */
  private langFor(sentence: string): string {
    if (this.replyLang === null && /\p{L}/u.test(sentence)) this.replyLang = scriptLanguage(sentence, this.o.language);
    return this.replyLang ?? this.o.language;
  }

  private async drain(): Promise<void> {
    this.cutIn = false;
    this.o.listener.setSpeaking(true);
    this.set({ phase: 'speaking' });
    try {
      while (this.queue.length > 0 && !this.cutIn && this.state.phase !== 'ended') {
        const sentence = this.queue.shift();
        if (sentence === undefined) break;
        await this.o.speaker.speak(sentence, this.langFor(sentence));
        const problem = this.o.speaker.takeProblem?.();
        if (problem) this.fail(problem);
        if (!this.cutIn) this.set({ spokenUpTo: this.state.spokenUpTo + 1 });
      }
    } finally {
      this.speaking = null;
      this.o.listener.setSpeaking(false);
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
