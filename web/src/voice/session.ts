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
 *
 * The end of a turn (docs/stories/12_assistant.md): every pause is recognised at once and scored
 * by the turn model. When it is sure he has finished, the utterance closes there and is sent with
 * no join window — about half a second sooner than the clock alone. When it is not sure, the clock
 * decides as before; words said after an early send join the run as a steer.
 */

import { SentenceSplitter, scriptLanguage, speakable } from './sentences';
import type { Cues } from './tones';

export type CallPhase = 'idle' | 'connecting' | 'listening' | 'transcribing' | 'thinking' | 'speaking' | 'ended';

/** However he pauses, what was recognised is sent no later than this after its first words. */
const JOIN_DEADLINE_MS = 1500;

/** The longest sentence before a tool step that is read out as where he has got to. */
const PROGRESS_MAX_CHARS = 120;

/** How many sentences of a reply have their voice fetched while the model is still writing.
 *  Two: the one he will hear first, and the one that must be ready when it ends. */
const PREFETCH_SENTENCES = 2;

/** What the model is told when he talked over its answer: what he heard, and not to say it again. */
const CUT_NOTE = (heard: number, total: number) =>
  `[The user cut you off: of your ${total} sentences he heard only the first ${heard}. Do not repeat what follows them — answer what he says now.]`;

/**
 * Where his voice comes out. The web cannot pick the earpiece or the speaker by name; what it
 * can choose is how the microphone is opened. On Android, a microphone with echo cancelling puts
 * Chrome in the phone's call mode — audio to the earpiece; one without it leaves the phone in its
 * normal mode — audio to the loudspeaker. Both hold the microphone throughout, so a cut-in by
 * voice works on either, through the echo gate (listener.ts).
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
  /** False while the socket to Jarvis is down: nothing said now will reach him. */
  online: boolean;
  startedAt: number;
}

export interface Listener {
  start(handlers: ListenerHandlers): Promise<void>;
  /** Close the utterance at the pause `at` now (the turn model is sure he finished); false when
   *  he has spoken since. Absent: the release closes it, as always. */
  finish?(at: number): boolean;
  stop(): void;
  /** Jarvis started or stopped speaking: the listener gates the microphone against his voice.
   *  `echoCancelled` says his voice is the page's
   *  own audio, which the browser subtracts from the microphone — a far lower bar to cut in. */
  setSpeaking(on: boolean, echoCancelled?: boolean): void;
  /** Resolves when the new route's audio is ready to play on. */
  setRoute(route: CallRoute): void | Promise<void>;
  setMuted(muted: boolean): void;
}

export interface ListenerHandlers {
  onLevel(level: number): void;
  onSpeechStart(): void;
  /** Silence that may be the end: the audio so far, to be recognised now rather than after the
   *  release. `at` names the pause; an utterance that ends there carries the same `at`.
   *  `offsetMs`: the audio before it is context for the turn model, already recognised. */
  onPause(audio: Blob, at: number, offsetMs?: number): void;
  /** He went on after the pause `at`, and what came before it is a piece of a long utterance:
   *  true when its words are on their way (recognised from that pause), so the rest is sent
   *  without it; false and it stays part of the rest. */
  onPiece?(at: number): boolean;
  /** An utterance has ended and its audio is on its way to be recognised. */
  onUtterance(audio: Blob, at?: number, offsetMs?: number): void;
  /** Something stopped him and came to nothing: a cough, a chair, a word too short to hear. */
  onSpeechDropped(): void;
  onError(message: string): void;
}

export interface Speaker {
  /** Say one sentence; resolves when it has been said (or was cut off). */
  speak(text: string, lang: string): Promise<void>;
  /** Stop mid-sentence and drop what was queued. */
  cancel(): void;
  /** Whether this device can speak at all (and in what). */
  available(): boolean;
  /** Start getting a sentence ready before its turn (a server voice fetches it now). `first`:
   *  the reply's first sentence, the one he waits for in silence — made faster if it can be. */
  prepare?(text: string, lang: string, first?: boolean): void;
  /** What went wrong with the last sentence, in a sentence for the screen — and cleared by
   *  the read. A voice that falls back silently is a call that "does not work". */
  takeProblem?(): string | null;
  /** A new reply is about to be spoken: a voice that had to fall back may try its own again. */
  beginReply?(): void;
  /** True when the page plays the audio itself, so the browser's echo canceller has it. */
  cancellable?(): boolean;
}

export type Recognised = { text: string; language: string | null; turnComplete?: number | null };

export interface TranscribeOptions {
  /** What was said just before: Whisper's context. */
  prompt?: string;
  /** Also score whether this is a finished turn. */
  turn?: boolean;
  /** Where in the audio the part to recognise begins (before it: context for the turn model). */
  offsetMs?: number;
}

export interface Transcriber {
  /** `language` is the call's current language, a hint for the recogniser — an utterance is
   *  too short to guess a language from, and a wrong guess comes back as Greek. */
  transcribe(audio: Blob, language: string, opts?: TranscribeOptions): Promise<Recognised>;
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
  /** How sure the turn model must be that he has finished for the utterance to be sent at
   *  once (`voice.turn_threshold`); null or absent = the clock alone decides. */
  turnThreshold?: number | null;
  onChange(state: CallState): void;
  /** The call's quiet signals (tones.ts); absent on a device that cannot make them. */
  cues?: Cues;
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
  online: true,
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
  /** What he was still to be told when a cut-in stopped the voice. Dropped for good once the
   *  cut-in turns into words; spoken after all if it came to nothing (2026-09-19: a cut-in that
   *  was heard but not understood left him listening to the rest of the old answer as if it
   *  were a new one). */
  private suspended: string[] = [];
  /** The sentence being spoken right now, so a cut-in can put it back unsaid. */
  private current: string | null = null;
  /** How much of the answer he had actually heard when he cut in. */
  private cutAfter = 0;
  /** Recognised words waiting for him to finish (the join window), and the timer that sends them. */
  private held = '';
  private heldSince = 0;
  private joinTimer: ReturnType<typeof setTimeout> | null = null;
  /** When the last utterance ended: the join window counts from there, not from when the
   *  recogniser answered, so recognition time is not paid twice. */
  private quietSince = 0;
  /** A recognition started at a pause, before the utterance was known to have ended. */
  private speculative: { at: number; result: Promise<Recognised | Error> } | null = null;
  /** The pieces of a long utterance recognised while he was still talking, in order, and the
   *  words of those already back (Whisper's prompt for the next one). */
  private pieces: Promise<Recognised | Error>[] = [];
  private piecesSaid: string[] = [];
  /** Words that arrived while the run was being created, steered once its id is known. */
  private steerWhenKnown = '';
  /** The language he is speaking, for the recogniser; the reply's, for the voice. Decided once
   *  per reply from its first sentence with letters, so one Latin-only sentence inside a
   *  Bulgarian answer does not switch the voice (2026-09-16, "the voices change"). */
  private heardLang: string;
  private replyLang: string | null = null;
  /** The current step's words, held until the step ends (see `onDelta`). */
  private stepText = '';
  /** He added words (a steer) while the run was writing an answer he had not heard yet. That
   *  answer is not read out: the core runs another step with his words in it (a late steer), and
   *  that one is. Kept aside in case the core finished instead - then it is said after all. */
  private steeredMidStep = false;
  private supersededText = '';
  /** A second splitter, reading the step as it is written, purely to start fetching the voice
   *  for a sentence before the step is known to be an answer. Nothing it produces is spoken. */
  private preview = new SentenceSplitter();
  private prefetched = 0;
  /** The language the step being written will be said in, as far as the preview can tell. */
  private previewLang: string | null = null;
  /** A route change is rebuilding the audio: sentences wait in the queue until it is done. */
  private rerouting = 0;
  /** The drain is being stopped for a route change, not a cut-in. */
  private rerouteStop = false;

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
    void this.o.listener.setRoute(route); // not started yet: only remembered
    try {
      await this.o.listener.start({
        onLevel: (level) => this.set({ level }),
        onSpeechStart: () => this.onSpeechStart(),
        onPause: (audio, at, offsetMs) => this.onPause(audio, at, offsetMs),
        onPiece: (at) => this.onPiece(at),
        onUtterance: (audio, at, offsetMs) => void this.onUtterance(audio, at, offsetMs),
        onSpeechDropped: () => this.onSpeechDropped(),
        onError: (message) => this.fail(message),
      });
    } catch (e) {
      this.fail(e instanceof Error ? e.message : 'The microphone could not be opened.');
      this.set({ phase: 'ended' });
      return;
    }
    // Hung up while the microphone was being asked for: it stays ended.
    if (this.state.phase !== 'ended') this.set({ phase: 'listening' });
  }

  end(): void {
    this.clearJoin();
    this.o.listener.stop();
    this.o.speaker.cancel();
    // A run still working is left to finish: its words land in the chat, only the reading
    // aloud stops. Cancelling it would throw away an answer that was already being written.
    this.queue = [];
    this.suspended = [];
    this.dropPieces();
    this.set({ phase: 'ended', level: 0 });
    this.o.cues?.close();
  }

  setMuted(muted: boolean): void {
    this.o.listener.setMuted(muted);
    if (muted) this.dropPieces();
    this.set({ muted });
  }

  /** What was being recognised of an utterance that will not be finished. */
  private dropPieces(): void {
    this.pieces = [];
    this.piecesSaid = [];
    this.speculative = null;
  }

  /**
   * Switch where his voice comes out. The listener closes and reopens the microphone and the
   * audio it plays on (listener.ts: the output's kind is fixed when it opens), so nothing may
   * play meanwhile: the sentence being said stops and is said again, from its start, on the new
   * route, and the waiting sound starts again for it (music at the ear, the pulse on the speaker).
   */
  async setRoute(route: CallRoute): Promise<void> {
    if (route === this.state.route || this.state.phase === 'ended') {
      await this.o.listener.setRoute(route);
      return;
    }
    this.set({ route });
    this.rerouting += 1;
    // The microphone is reopened: an utterance he was in the middle of is gone with it, and its
    // pieces must not be glued to the front of the next one.
    this.dropPieces();
    this.o.cues?.thinking(false);
    let unsaid: string[] = [];
    if (this.speaking) {
      unsaid = this.current ? [this.current, ...this.queue] : [...this.queue];
      this.queue = [];
      this.rerouteStop = true;
      this.o.speaker.cancel();
      await this.speaking;
      this.rerouteStop = false;
    }
    try {
      await this.o.listener.setRoute(route);
    } finally {
      this.rerouting -= 1;
    }
    if ((this.state.phase as CallPhase) === 'ended') return; // hung up while the audio was rebuilt
    this.queue = [...unsaid, ...this.queue];
    if (this.rerouting > 0) return; // switched again meanwhile: the last switch resumes
    if (this.queue.length > 0) this.startDrain();
    else if (this.state.phase === 'thinking') this.o.cues?.thinking(true);
    else if (this.state.phase === 'speaking') this.set({ phase: this.state.runId ? 'thinking' : 'listening' });
  }

  // --- from the listener -------------------------------------------------------------------

  private onSpeechStart(): void {
    if (this.state.phase === 'speaking') {
      // Cut-in: he stops mid-sentence and listens. What was half-written before the cut is
      // dropped too — it was an answer to the question he has just been talked out of.
      this.cutIn = true;
      this.o.speaker.cancel();
      // Held, not dropped: if this turns out to be a cough the rest of the answer goes on.
      this.suspended = this.current ? [this.current, ...this.queue] : [...this.queue];
      this.cutAfter = this.state.spokenUpTo;
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
  /**
   * A cut-in that came to nothing: say the rest of what he was being told. The drain it
   * interrupted is still unwinding (its sentence resolves a microtask later), so this waits for
   * it — starting a second drain beside the first would say everything twice.
   */
  onSpeechDropped(): void {
    if (this.state.phase === 'ended' || this.suspended.length === 0 || this.held) return;
    const rest = this.suspended;
    this.suspended = [];
    void (async () => {
      await this.speaking;
      if (this.state.phase === 'ended' || this.held || this.suspended.length > 0) return;
      this.queue = [...rest, ...this.queue];
      this.startDrain();
    })();
  }

  private onPause(audio: Blob, at: number, offsetMs?: number): void {
    if (this.state.phase === 'ended' || this.state.muted) return;
    const result = this.recognise(audio, offsetMs);
    this.speculative = { at, result };
    // Sure he has finished: close the utterance at this pause rather than wait out the release.
    void result.then((r) => {
      if (r instanceof Error || !this.confident(r)) return;
      if (this.speculative?.at !== at || this.state.phase === 'ended' || this.state.muted) return;
      this.o.listener.finish?.(at);
    });
  }

  /** A pause he went on after: what was recognised from it is a piece of this utterance. */
  private onPiece(at: number): boolean {
    if (this.speculative?.at !== at || this.state.phase === 'ended') return false;
    const piece = this.speculative.result;
    this.speculative = null;
    this.pieces.push(piece);
    void piece.then((r) => {
      if (!(r instanceof Error) && r.text.trim()) this.piecesSaid.push(r.text.trim());
    });
    return true;
  }

  private recognise(audio: Blob, offsetMs?: number): Promise<Recognised | Error> {
    // His own words so far in this utterance, and nothing else. What Jarvis last said was tried
    // (2026-10-13) and dropped: it is English while he speaks Bulgarian, it fixed nothing measurable,
    // and recognition was worse that evening - a prompt in another language is a known way to
    // make Whisper invent words.
    const prompt = this.piecesSaid.join(' ');
    const opts: TranscribeOptions = { turn: this.o.turnThreshold != null };
    if (prompt) opts.prompt = prompt;
    if (offsetMs) opts.offsetMs = offsetMs;
    return this.o.transcriber.transcribe(audio, this.heardLang, opts).then(
      (r): Recognised | Error => r,
      (e: unknown): Recognised | Error => (e instanceof Error ? e : new Error(String(e))),
    );
  }

  private confident(r: Recognised): boolean {
    const bar = this.o.turnThreshold;
    return bar != null && r.turnComplete != null && r.turnComplete >= bar && r.text.trim() !== '';
  }

  private async onUtterance(audio: Blob, at?: number, offsetMs?: number): Promise<void> {
    if (this.state.phase === 'ended' || this.state.muted) return;
    this.quietSince = this.now();
    this.set({ phase: 'transcribing', problem: null });
    // The utterance ended at the pause already being recognised: that answer is this answer.
    // Otherwise the rest is recognised now - asked before the pieces are let go, since their
    // words are its prompt.
    const early = at !== undefined && this.speculative?.at === at ? this.speculative.result : null;
    this.speculative = null;
    const rest = early ?? this.recognise(audio, offsetMs);
    // The pieces recognised while he talked belong to this utterance and to no later one.
    const pieces = this.pieces;
    this.pieces = [];
    this.piecesSaid = [];
    let text = '';
    let finished = false;
    try {
      const res = await rest;
      if (res instanceof Error) throw res;
      const said: string[] = [];
      for (const piece of await Promise.all(pieces)) {
        if (piece instanceof Error) throw piece;
        if (piece.text.trim()) said.push(piece.text.trim());
      }
      text = [...said, res.text.trim()].filter(Boolean).join(' ');
      finished = this.confident(res);
      // He may switch to another of the call's languages — believed only when the words are
      // written in that language's script too. Whisper labels a noise "en" as readily as
      // anything, and a hint of "en" on Bulgarian speech comes back as an English translation.
      if (res.language && this.o.languages?.includes(res.language) && text && scriptLanguage(text, res.language) === res.language) {
        this.heardLang = res.language;
      }
    } catch (e) {
      if ((this.state.phase as CallPhase) === 'ended') return;
      this.fail(`I can't hear you right now — the speech service is not answering (${e instanceof Error ? e.message : 'error'}).`);
      this.set({ phase: 'listening' });
      return;
    }
    // Hung up while Whisper worked: nothing more happens on this call, nothing is sent.
    if ((this.state.phase as CallPhase) === 'ended') return;
    if (!text) {
      this.set({ phase: this.held ? 'transcribing' : 'listening' });
      this.onSpeechDropped();
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
    // recogniser worked counts toward it. None at all when the turn model is sure he is done.
    const wait = finished
      ? 0
      : Math.max(0, Math.min(join - (this.now() - this.quietSince), this.heldSince + JOIN_DEADLINE_MS - this.now()));
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
    // His words won: the rest of the answer he talked over is not said after all.
    const interrupted = this.suspended.length > 0;
    const heardOf = this.cutAfter;
    const ofTotal = this.cutAfter + this.suspended.length;
    this.suspended = [];
    this.set({ heard: text, phase: 'thinking', saying: [], spokenUpTo: 0 });
    this.o.cues?.accepted();
    // A run still working gets the words; otherwise a new voice turn. If the run finished
    // between the two, the core turns the steer into a new turn itself.
    // A steer that follows a cut-in says so: the model wrote an answer of which he heard the
    // first sentences only, and without being told it simply said the whole thing again
    // (2026-09-19, "просто ми повтори първия отговор").
    const outgoing = interrupted ? `${CUT_NOTE(heardOf, ofTotal)}\n${text}` : text;
    // Talking on while the answer was being written: that answer is about half of what he said.
    if (this.state.runId && !interrupted) this.steeredMidStep = true;
    const sent = this.state.runId ? this.o.transport.steer(this.state.runId, outgoing) : this.create(outgoing);
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
    this.previewLang = null;
    this.o.speaker.beginReply?.();
    this.set({ runId });
    if (this.steerWhenKnown) {
      const more = this.steerWhenKnown;
      this.steerWhenKnown = '';
      this.steeredMidStep = true;
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
    // A new step is being written: the core went on with his words, the stale answer is gone.
    this.supersededText = '';
    this.stepText += text;
    // The words are still held until the step ends — a step that turns out to be tool calls has
    // written a plan, not a reply. But the VOICE for the first sentences can be fetched now, so
    // that when the step does end there is nothing left to wait for: a second of synthesis moves
    // out of the silence and into the time the model is still writing (2026-09-20).
    if (this.prefetched >= PREFETCH_SENTENCES) return;
    for (const sentence of this.preview.push(text)) {
      const clean = speakable(sentence);
      if (!clean) continue;
      // Nothing of this reply queued yet: this sentence is the one he will wait for.
      const first = this.prefetched === 0 && this.state.saying.length === 0;
      this.prefetched += 1;
      // The language it will be SAID in: the reply's (see langFor), decided by the step's first
      // sentence when the reply has none yet - not each sentence's own. Fetched in its own
      // language, a sentence in the other script was fetched twice, the second time when it
      // was due, and the wasted one held one of the core's two synthesis slots (2026-10-15).
      // langFor itself is not asked: a step that turns out to be tools must not decide the reply.
      this.previewLang ??= this.replyLang ?? (/\p{L}/u.test(clean) ? scriptLanguage(clean, this.o.language) : null);
      this.o.speaker.prepare?.(clean, this.previewLang ?? this.o.language, first);
      if (this.prefetched >= PREFETCH_SENTENCES) break;
    }
  }

  /** Say something that is not a run's reply - an answered ring's result - then listen. */
  say(text: string): void {
    if (this.state.phase === 'ended') return;
    const splitter = new SentenceSplitter();
    for (const sentence of splitter.push(text)) this.enqueue(sentence);
    const rest = splitter.flush();
    if (rest) this.enqueue(rest);
  }

  onStepDone(runId: string, endedInToolCalls: boolean): void {
    if (runId !== this.state.runId || this.state.phase === 'ended') return;
    const text = this.stepText;
    this.stepText = '';
    this.preview = new SentenceSplitter();
    this.prefetched = 0;
    this.previewLang = null;
    if (endedInToolCalls) {
      this.steeredMidStep = false; // his words go into the next step anyway
      // Where he has got to: the one sentence written before the tools ("Checking your mail."),
      // read out while he waits, like the in-between replies in the chat (2026-10-14). Only
      // the first sentence, and only a short one - the rest of a tool step is its working.
      const lead = new SentenceSplitter();
      const first = lead.push(text)[0] ?? lead.flush();
      if (first && first.length <= PROGRESS_MAX_CHARS) this.enqueue(first);
      return;
    }
    if (!text) return;
    if (this.steeredMidStep) {
      // An answer to what he said before he went on: held back, not read out.
      this.steeredMidStep = false;
      this.supersededText = text;
      return;
    }
    for (const sentence of this.splitter.push(text)) this.enqueue(sentence);
    const rest = this.splitter.flush();
    if (rest) this.enqueue(rest);
  }

  onRunDone(runId: string): void {
    if (runId !== this.state.runId) return;
    // The core finished without another step (his words came too late and start a run of their
    // own): the held answer is all there is to what he asked first, so it is said.
    if (this.supersededText) {
      this.stepText = this.supersededText + this.stepText;
      this.supersededText = '';
    }
    this.steeredMidStep = false;
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
    if (runId !== this.state.runId || this.state.phase === 'ended') return;
    this.set({ runId: null });
    this.queue = [];
    this.o.speaker.cancel();
    this.fail(`Something went wrong on my side: ${error}`);
    void this.sayProblem();
  }

  // --- speaking ---------------------------------------------------------------------------

  private enqueue(sentence: string): void {
    const clean = speakable(sentence);
    if (!clean) return;
    const first = this.state.saying.length === 0;
    this.queue.push(clean);
    // Only the next few are fetched ahead (the drain fetches the rest as it goes). All of a long
    // reply at once filled the core's two synthesis slots, and after a cut-in the new answer's
    // first sentence waited behind the old answer's, which nobody would hear.
    if (this.queue.length <= PREFETCH_SENTENCES) this.o.speaker.prepare?.(clean, this.langFor(clean), first);
    this.set({ saying: [...this.state.saying, clean] });
    this.startDrain();
  }

  /** The reply's language: decided by its first sentence that has letters, then kept. */
  private langFor(sentence: string): string {
    if (this.replyLang === null && /\p{L}/u.test(sentence)) this.replyLang = scriptLanguage(sentence, this.o.language);
    return this.replyLang ?? this.o.language;
  }

  private async drain(): Promise<void> {
    this.cutIn = false;
    let echoCancelled = this.o.speaker.cancellable?.() ?? false;
    this.o.listener.setSpeaking(true, echoCancelled);
    this.set({ phase: 'speaking' });
    try {
      while (this.queue.length > 0 && !this.cutIn && this.state.phase !== 'ended') {
        const sentence = this.queue.shift();
        if (sentence === undefined) break;
        this.current = sentence;
        // While this one plays, the one that has just come within reach gets ready.
        const ahead = this.queue[PREFETCH_SENTENCES - 1];
        if (ahead !== undefined) this.o.speaker.prepare?.(ahead, this.langFor(ahead));
        // The bar to cut in follows the voice actually playing: once the server voice has fallen
        // back to the device's (outside the echo canceller), the low bar let Jarvis's own words
        // through as a cut-in and he interrupted himself.
        const cancelled = this.o.speaker.cancellable?.() ?? false;
        if (cancelled !== echoCancelled) {
          echoCancelled = cancelled;
          this.o.listener.setSpeaking(true, cancelled);
        }
        await this.o.speaker.speak(sentence, this.langFor(sentence));
        this.current = null;
        const problem = this.o.speaker.takeProblem?.();
        if (problem) this.fail(problem);
        if (!this.cutIn && !this.rerouteStop) this.set({ spokenUpTo: this.state.spokenUpTo + 1 });
      }
    } finally {
      this.speaking = null;
      this.current = null;
      this.o.listener.setSpeaking(false);
    }
    if (this.state.phase === 'ended') return;
    if (this.cutIn) return; // already listening; the cut-in's words are on their way
    if (this.rerouteStop) return; // the route change says the rest once the audio is back
    if (this.state.runId) this.set({ phase: 'thinking' }); // more is being written
    else this.backToListening();
  }

  /** Say what is queued - unless a route change is rebuilding the audio; it starts this itself. */
  private startDrain(): void {
    if (this.rerouting > 0) return;
    this.speaking ??= this.drain();
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
    // The answer it cut short is still unwinding: let it, or its ending would ungate the
    // microphone in the middle of this sentence and his own words come back as a question.
    await this.speaking;
    if (this.state.phase === 'ended') return;
    this.o.listener.setSpeaking(true, this.o.speaker.cancellable?.() ?? false);
    this.set({ phase: 'speaking' });
    try {
      await this.o.speaker.speak(p, scriptLanguage(p, this.o.language));
    } finally {
      this.o.listener.setSpeaking(false);
      this.backToListening();
    }
  }

  private fail(message: string): void {
    this.set({ problem: message });
  }

  /** The line to Jarvis, as the socket sees it: a call on a dead socket says so, quietly. */
  setOnline(online: boolean): void {
    if (this.state.phase === 'ended' || online === this.state.online) return;
    this.set({ online });
    this.o.cues?.offline(!online);
  }

  private set(patch: Partial<CallState>): void {
    const before = this.state.phase;
    this.state = { ...this.state, ...patch };
    // While a route change rebuilds the audio there is nothing to play the waiting sound on; it
    // starts again when the new route is up.
    if (this.state.phase !== before && this.rerouting === 0) this.o.cues?.thinking(this.state.phase === 'thinking');
    this.o.onChange(this.state);
  }
}
