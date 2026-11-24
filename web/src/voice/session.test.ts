import { describe, expect, it, vi } from 'vitest';
import {
  CallSession,
  type CallState,
  type Listener,
  type ListenerHandlers,
  type Recognised,
  type Speaker,
  type Transcriber,
  type TranscribeOptions,
  type Transport,
} from './session';
import type { Cues } from './tones';
import { callTrace } from './trace';

/** A listener the test drives by hand. */
class FakeListener implements Listener {
  h: ListenerHandlers | null = null;
  gated = false;
  route = 'earpiece';
  muted = false;
  stopped = false;
  failStart = false;
  start(h: ListenerHandlers): Promise<void> {
    if (this.failStart) return Promise.reject(new Error('Permission denied'));
    this.h = h;
    return Promise.resolve();
  }
  stop(): void {
    this.stopped = true;
  }
  setSpeaking(g: boolean): void {
    this.gated = g;
  }
  /** Set by a test to hold a route change open (the real one reopens the microphone and audio). */
  routeGate: Promise<void> | null = null;
  /** An utterance he is in the middle of: a mute or a route change closes it as the real one does. */
  open = false;
  private flush(): void {
    if (!this.open) return;
    this.open = false;
    this.h?.onUtterance(new Blob(['open']), undefined, 0, 0);
  }
  setRoute(r: 'earpiece' | 'speaker'): Promise<void> {
    this.route = r;
    this.flush();
    return this.routeGate ?? Promise.resolve();
  }
  setMuted(m: boolean): void {
    if (m && !this.muted) this.flush();
    this.muted = m;
  }
  turnsSent = 0;
  turnSent(): void {
    this.turnsSent += 1;
  }
  finished: number[] = [];
  /** finish() closes the utterance the way the real listener does: onUtterance at that pause. */
  finish(at: number): boolean {
    this.finished.push(at);
    this.h?.onUtterance(new Blob(['tail']), at);
    return true;
  }
  say(): void {
    this.h?.onSpeechStart();
    this.h?.onUtterance(new Blob(['x']));
  }
  /** Something stopped him and came to nothing: a cough, a chair. */
  cough(): void {
    this.h?.onSpeechStart();
    this.h?.onSpeechDropped();
  }
  /** Sound over the floor the segmenter has not yet called speech; and that it came to nothing. */
  onset(): void {
    this.h?.onSoundStart?.();
  }
  quiet(): void {
    this.h?.onSoundEnd?.();
  }
}

/** A speaker that finishes when the test says so. */
class FakeSpeaker implements Speaker {
  spoken: { text: string; lang: string }[] = [];
  prepared: string[] = [];
  firsts: string[] = [];
  cancelled = 0;
  private pending: (() => void)[] = [];
  /** `lang|text`, the key a server voice caches a prepared sentence by. */
  preparedKeys: string[] = [];
  prepare(text: string, lang: string, first?: boolean): void {
    this.prepared.push(text);
    this.preparedKeys.push(`${lang}|${text}`);
    if (first) this.firsts.push(text);
  }
  available(): boolean {
    return true;
  }
  speak(text: string, lang: string): Promise<void> {
    this.spoken.push({ text, lang });
    return new Promise((r) => this.pending.push(r));
  }
  cancel(): void {
    this.cancelled += 1;
    this.finishAll();
  }
  finishOne(): void {
    this.pending.shift()?.();
  }
  finishAll(): void {
    const p = this.pending;
    this.pending = [];
    p.forEach((r) => r());
  }
}

class FakeTranscriber implements Transcriber {
  next = 'какво ще кажеш';
  fail = false;
  hints: string[] = [];
  language: string | null = 'bg';
  /** How long a recognition takes; 0 is the next turn of the loop. */
  delayMs = 0;
  calls = 0;
  /** What the turn model says; null = it cannot say. */
  turn: number | null = null;
  opts: (TranscribeOptions | undefined)[] = [];
  /** Answers by the audio's text, for tests that send pieces; and which of them fail, or take how long. */
  byAudio: Record<string, string> = {};
  failAudio: string[] = [];
  delayByAudio: Record<string, number> = {};
  async transcribe(audio: Blob, language: string, opts?: TranscribeOptions): Promise<Recognised> {
    this.hints.push(language);
    this.opts.push(opts);
    this.calls += 1;
    if (this.fail) return Promise.reject(new Error('502'));
    const byKey = Object.keys(this.byAudio).length || this.failAudio.length || Object.keys(this.delayByAudio).length;
    const key = byKey ? await audio.text() : "";
    if (this.failAudio.includes(key)) return Promise.reject(new Error('502'));
    const res = { text: this.byAudio[key] ?? this.next, language: this.language, turnComplete: this.turn };
    const delay = this.delayByAudio[key] ?? this.delayMs;
    return delay ? new Promise((r) => setTimeout(() => r(res), delay)) : Promise.resolve(res);
  }
}

class FakeCues implements Cues {
  log: string[] = [];
  accepted(): void {
    this.log.push('accepted');
  }
  thinking(on: boolean): void {
    this.log.push(`thinking:${on}`);
  }
  offline(on: boolean): void {
    this.log.push(`offline:${on}`);
  }
  close(): void {
    this.log.push('close');
  }
}

class FakeTransport implements Transport {
  created: string[] = [];
  steered: { runId: string; text: string }[] = [];
  down = false;
  create(text: string): boolean {
    if (this.down) return false;
    this.created.push(text);
    return true;
  }
  steer(runId: string, text: string): boolean {
    if (this.down) return false;
    this.steered.push({ runId, text });
    return true;
  }
  cancelled: string[] = [];
  cancel(runId: string): void {
    this.cancelled.push(runId);
  }
}

function build(
  over: Partial<{
    listener: FakeListener;
    speaker: FakeSpeaker;
    transcriber: FakeTranscriber;
    transport: FakeTransport;
    minWaitMs: number;
    patienceMs: number;
    turnThreshold: number | null;
    ceilingMs: number;
    hangUpMs: number;
  }> = {},
) {
  const listener = over.listener ?? new FakeListener();
  const speaker = over.speaker ?? new FakeSpeaker();
  const transcriber = over.transcriber ?? new FakeTranscriber();
  const transport = over.transport ?? new FakeTransport();
  const cues = new FakeCues();
  const states: CallState[] = [];
  const session = new CallSession({
    listener,
    speaker,
    transcriber,
    transport,
    language: 'bg',
    languages: ['bg', 'en'],
    // No wait unless a test is about the wait: most tests are about what follows a turn.
    minWaitMs: over.minWaitMs ?? 0,
    patienceMs: over.patienceMs ?? 0,
    limits: { ...(over.ceilingMs !== undefined ? { ceilingMs: over.ceilingMs } : {}), ...(over.hangUpMs !== undefined ? { hangUpMs: over.hangUpMs } : {}) },
    turnThreshold: over.turnThreshold ?? null,
    conversationId: 'c1',
    cues,
    onChange: (s) => states.push(s),
  });
  return { session, listener, speaker, transcriber, transport, cues, states, phases: () => states.map((s) => s.phase) };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
/** Transcription resolves, then the (zero) join window elapses: two turns of the event loop. */
const settle = async () => {
  await tick();
  await tick();
};

describe('CallSession', () => {
  it('walks a whole turn: listen → transcribe → think → speak sentence by sentence → listen', async () => {
    const t = build();
    await t.session.start();
    expect(t.session.state.phase).toBe('listening');
    t.listener.say();
    await settle();
    expect(t.session.state.phase).toBe('thinking');
    expect(t.session.state.heard).toBe('какво ще кажеш');
    expect(t.transport.created).toEqual(['какво ще кажеш']);

    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Три дни. Има ли');
    await tick();
    expect(t.session.state.phase).toBe('thinking'); // a step's words wait for the step to end
    expect(t.speaker.spoken).toEqual([]);
    t.session.onDelta('r1', ' бучка?');
    t.session.onStepDone('r1', false);
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    expect(t.listener.gated).toBe(true);
    expect(t.speaker.spoken).toEqual([{ text: 'Три дни.', lang: 'bg' }]);
    t.speaker.finishOne();
    await tick();
    expect(t.session.state.spokenUpTo).toBe(1);
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Три дни.', 'Има ли бучка?']);
    t.session.onRunDone('r1');
    t.speaker.finishOne();
    await tick();
    expect(t.session.state.phase).toBe('listening');
    expect(t.listener.gated).toBe(false);
    expect(t.session.state.runId).toBeNull();
  });

  it('a step that ends in tool calls is read out only as its first short sentence: where he has got to', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Checking your calendar. I will look at Friday and then the weekend for a slot that fits. ');
    t.session.onStepDone('r1', true);
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Checking your calendar.']);
    t.speaker.finishAll();
    await tick();
    expect(t.session.state.phase).toBe('thinking'); // still working: back to waiting (and the music)
    t.session.onDelta('r1', 'Три дни. ');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Checking your calendar.', 'Три дни.']);
  });

  it('a cut-in while he speaks stops him and steers the run that is still working', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето. И');
    t.session.onStepDone('r1', false);
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    // Arsen talks over him.
    t.transcriber.next = 'не, не това';
    t.listener.say();
    expect(t.speaker.cancelled).toBe(1);
    await settle();
    // The run is still working: the words go to it, not to a new run — and they carry a note
    // saying he heard only the first sentence, or the model says the whole answer again.
    expect(t.transport.steered).toHaveLength(1);
    expect(t.transport.steered[0]?.runId).toBe('r1');
    expect(t.transport.steered[0]?.text).toContain('не, не това');
    expect(t.transport.steered[0]?.text).toMatch(/cut you off/);
    expect(t.transport.created).toHaveLength(1);
    expect(t.session.state.phase).toBe('thinking');
    // Its new sentences are spoken from a clean queue.
    t.session.onDelta('r1', ' Разбрах. Ще го направя.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.spoken.map((s) => s.text).slice(-2)).toEqual(['Разбрах.', 'Ще го направя.']);
    // "И" — the half-sentence written before the cut — was never said.
    expect(t.speaker.spoken.some((s) => s.text === 'И')).toBe(false);
  });

  it('the voice of the first sentences is fetched while the model is still writing', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    // Mid-step: nothing may be SAID yet (the step could still turn out to be tool calls), but
    // the synthesis of what is already written need not wait for it.
    t.session.onDelta('r1', 'Готово. Всичко мина. И още');
    expect(t.speaker.prepared).toEqual(['Готово.', 'Всичко мина.']);
    expect(t.speaker.spoken).toEqual([]);
    t.session.onStepDone('r1', false);
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Готово.']);
  });

  it('a step that was a plan prepares a voice nobody hears, and says nothing', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Трябва да проверя календара, като първо прегледам цялата седмица, после уикенда, после следващата седмица и чак тогава ще знам. Нека погледна.');
    t.session.onStepDone('r1', true); // tool calls: a long first sentence is working, not news
    await tick();
    expect(t.speaker.spoken).toEqual([]);
    // ...and the next step starts its prefetch afresh (a sentence is only sure once something
    // follows it, which is why the splitter holds the last one until the step ends).
    t.session.onDelta('r1', 'Утре в девет. И');
    expect(t.speaker.prepared.at(-1)).toBe('Утре в девет.');
  });

  it('a cut-in that comes to nothing lets him finish what he was saying', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    t.listener.cough(); // a chair, a cough: he stops, and nothing was said
    expect(t.speaker.cancelled).toBe(1);
    expect(t.session.state.phase).toBe('listening');
    await tick();
    // The rest of the answer is said after all, starting with the sentence he talked over.
    expect(t.session.state.phase).toBe('speaking');
    for (let i = 0; i < 4; i += 1) {
      t.speaker.finishAll();
      await tick();
    }
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Първо.', 'Първо.', 'Второ.', 'Трето.']);
    expect(t.transport.steered).toEqual([]);
  });

  it('a cut-in with words in it drops the rest of the answer for good', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    t.transcriber.next = 'чакай';
    t.listener.say();
    await settle();
    t.speaker.finishAll();
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Първо.']);
    expect(t.transport.created).toHaveLength(2);
    expect(t.transport.created[1]).toContain('чакай');
  });

  it('the quiet signals: his words accepted, the thinking, the line going down and up', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    expect(t.cues.log).toContain('accepted');
    expect(t.cues.log).toContain('thinking:true');
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Готово.');
    t.session.onStepDone('r1', false);
    await tick();
    expect(t.cues.log).toContain('thinking:false'); // he is speaking now, not thinking
    t.session.setOnline(false);
    t.session.setOnline(false); // said once, not once a second
    expect(t.cues.log.filter((l) => l === 'offline:true')).toHaveLength(1);
    expect(t.session.state.online).toBe(false);
    t.session.setOnline(true);
    expect(t.cues.log).toContain('offline:false');
    t.session.end();
    expect(t.cues.log.at(-1)).toBe('close');
  });

  it('with no run working, a new utterance starts a new voice turn in the same conversation', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Да.');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishAll();
    await tick();
    expect(t.session.state.phase).toBe('listening');
    t.transcriber.next = 'и още нещо';
    t.listener.say();
    await settle();
    expect(t.transport.created).toEqual(['какво ще кажеш', 'и още нещо']);
    expect(t.transport.steered).toEqual([]);
  });

  it('a second utterance before the run id arrives is not a second turn', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.transcriber.next = 'и още нещо';
    t.listener.say(); // the run has not been queued yet
    await settle();
    expect(t.transport.created).toEqual(['какво ще кажеш']);
    // Held until the run has an id, then handed to it — one turn, nothing lost, nothing doubled
    // (two creates used to be answered twice, spoken over each other).
    t.session.onRunQueued('r1', 'c1');
    expect(t.transport.steered).toEqual([{ runId: 'r1', text: 'и още нещо' }]);
  });

  it('a pause for breath does not end the turn: what follows joins the same utterance', async () => {
    const t = build({ patienceMs: 30 });
    await t.session.start();
    t.listener.say();
    await tick(); // recognised, now waiting for him to go on
    t.transcriber.next = 'в крайна сметка';
    t.listener.say(); // he went on inside the window
    await new Promise((r) => setTimeout(r, 60));
    expect(t.transport.created).toEqual(['какво ще кажеш в крайна сметка']);
    expect(t.states.at(-1)?.heard).toBe('какво ще кажеш в крайна сметка');
  });

  it('a pause that turns out to be the end: the words recognised at the pause are the words sent', async () => {
    const t = build();
    await t.session.start();
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['x']), 500); // silence began at 500 ms: recognise now
    t.transcriber.next = 'wrong, if asked again';
    t.listener.h?.onUtterance(new Blob(['x']), 500); // the release confirmed it: same `at`
    await settle();
    expect(t.transcriber.calls).toBe(1);
    expect(t.transport.created).toEqual(['какво ще кажеш']);
  });

  it('a pause he talked through is forgotten: the whole utterance is recognised afresh', async () => {
    const t = build();
    await t.session.start();
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['x']), 500);
    t.transcriber.next = 'какво ще кажеш в крайна сметка';
    t.listener.h?.onUtterance(new Blob(['xy']), 900); // he went on; the end is a later silence
    await settle();
    expect(t.transcriber.calls).toBe(2);
    expect(t.transport.created).toEqual(['какво ще кажеш в крайна сметка']);
  });

  it('a recognition that fails at the pause is not the end of the turn', async () => {
    const t = build();
    await t.session.start();
    t.transcriber.fail = true;
    t.listener.h?.onPause(new Blob(['x']), 500);
    await tick();
    t.transcriber.fail = false;
    t.listener.h?.onUtterance(new Blob(['x']), 500);
    await settle();
    expect(t.states.at(-1)?.problem).toMatch(/can't hear you/);
    expect(t.phases().at(-1)).toBe('listening');
  });

  it('the wait counts from the start of the silence, not from when the recogniser answered', async () => {
    const t = build({ patienceMs: 80 });
    t.transcriber.delayMs = 40;
    await t.session.start();
    t.listener.h?.onSpeechStart();
    // The release had run 60 ms of the silence already when the utterance closed.
    t.listener.h?.onUtterance(new Blob(['x']), undefined, 0, 60);
    await new Promise((r) => setTimeout(r, 70));
    // From the utterance's close it would be 80 ms; after recognition, 40 + 80. From the silence: 40.
    expect(t.transport.created).toEqual(['какво ще кажеш']);
  });

  it('a noisy room does not hold the turn: a sound with no words goes on with the time that was left', async () => {
    // The 2026-09-16 rule kept: only recognised words extend the wait ("it catches every sound but not me").
    const t = build({ patienceMs: 120 });
    await t.session.start();
    t.listener.say();
    await tick();
    const t0 = Date.now();
    for (let i = 0; i < 3; i += 1) {
      await new Promise((r) => setTimeout(r, 20));
      t.listener.cough(); // something in the room
    }
    t.transcriber.next = ''; // and a longer sound Whisper finds nothing in
    t.listener.say();
    await new Promise((r) => setTimeout(r, Math.max(0, 90 - (Date.now() - t0))));
    expect(t.transport.created).toEqual([]); // still inside the patience
    await new Promise((r) => setTimeout(r, Math.max(0, 150 - (Date.now() - t0))));
    // Sent at the first patience's end, not 120 ms after the last sound.
    expect(t.transport.created).toEqual(['какво ще кажеш']);
  });

  it('a sound that turns out to be words holds the turn until they are in', async () => {
    const t = build({ patienceMs: 40 });
    await t.session.start();
    t.listener.say();
    await tick();
    t.transcriber.delayMs = 80; // he goes on, and Whisper takes longer than the patience
    t.transcriber.next = 'и освен това';
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['x']), 100);
    await new Promise((r) => setTimeout(r, 100));
    expect(t.transport.created).toEqual([]); // words: the clock stood still
    t.transcriber.delayMs = 0;
    t.listener.h?.onUtterance(new Blob(['x']), 100, 0, 500);
    await new Promise((r) => setTimeout(r, 30));
    expect(t.transport.created).toEqual(['какво ще кажеш и освен това']);
  });

  it('the recogniser gets the call language as a hint, and follows him when he switches', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    expect(t.transcriber.hints).toEqual(['bg']);
    t.transcriber.language = 'en'; // labelled English, but the words are Cyrillic: not believed
    t.listener.say();
    await settle();
    t.transcriber.next = 'switch to english please';
    t.listener.say();
    await settle();
    t.transcriber.language = 'el'; // not one of the call's languages: not believed
    t.listener.say();
    await settle();
    t.listener.say();
    await settle();
    expect(t.transcriber.hints).toEqual(['bg', 'bg', 'bg', 'en', 'en']);
  });

  it('says one honest sentence when Whisper is down and keeps listening', async () => {
    const t = build();
    await t.session.start();
    t.transcriber.fail = true;
    t.listener.say();
    await settle();
    expect(t.session.state.phase).toBe('listening');
    expect(t.session.state.problem).toMatch(/speech service is not answering/);
    expect(t.transport.created).toEqual([]);
  });

  it('a failed run is said out loud, then listening resumes', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onRunFailed('r1', 'model unreachable');
    await tick();
    expect(t.speaker.spoken.at(-1)?.text).toMatch(/model unreachable/);
    t.speaker.finishAll();
    await tick();
    expect(t.session.state.phase).toBe('listening');
  });

  it('a refused microphone ends the call with the reason on screen', async () => {
    const listener = new FakeListener();
    listener.failStart = true;
    const t = build({ listener });
    await t.session.start();
    expect(t.session.state.phase).toBe('ended');
    expect(t.session.state.problem).toBe('Permission denied');
  });

  it('hanging up stops the mic and the voice and drops the queue', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Едно. Две. Три. ');
    t.session.onStepDone('r1', false);
    await tick();
    t.session.end();
    expect(t.listener.stopped).toBe(true);
    expect(t.speaker.cancelled).toBe(1);
    expect(t.session.state.phase).toBe('ended');
    t.session.onDelta('r1', 'Четири. Пет.'); // late deltas change nothing
    t.session.onStepDone('r1', false);
    expect(t.speaker.spoken).toHaveLength(1);
  });

  it('the route is the listener to act on, and the screen to show', async () => {
    const t = build();
    await t.session.start();
    expect(t.listener.route).toBe('earpiece');
    expect(t.session.state.route).toBe('earpiece');
    void t.session.setRoute('speaker');
    expect(t.listener.route).toBe('speaker');
    expect(t.session.state.route).toBe('speaker');
  });

  it('a route change mid-sentence says that sentence again on the new route, and nothing before it is ready', async () => {
    // 2026-10-15: the audio is rebuilt on a route change (the output's kind is fixed when it opens).
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето.');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishOne(); // "Първо." said
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Първо.', 'Второ.']);
    let open: () => void = () => undefined;
    t.listener.routeGate = new Promise((r) => (open = r));
    const switching = t.session.setRoute('speaker');
    await tick();
    // "Второ." was cut by the switch: not counted as heard, and nothing plays while the audio is down.
    expect(t.session.state.spokenUpTo).toBe(1);
    expect(t.speaker.spoken).toHaveLength(2);
    open();
    await switching;
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Първо.', 'Второ.', 'Второ.']);
    t.speaker.finishOne();
    await tick();
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.spoken.map((s) => s.text).slice(3)).toEqual(['Трето.']);
    expect(t.session.state.spokenUpTo).toBe(3);
    expect(t.session.state.phase).toBe('listening');
  });

  it('a route change while he thinks starts the waiting sound again once the new route is up', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    expect(t.session.state.phase).toBe('thinking');
    t.cues.log = [];
    await t.session.setRoute('speaker');
    expect(t.cues.log).toEqual(['thinking:false', 'thinking:true']);
  });

  it('one reply, one voice: the first sentence with letters decides the language for the rest', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Готово. Run it with vLLM. ');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.spoken.map((s) => s.lang)).toEqual(['bg', 'bg']);
    // The next reply decides afresh.
    t.speaker.finishAll();
    await tick();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r2', 'c1');
    t.session.onDelta('r2', 'Done. Готово е. ');
    t.session.onRunDone('r2');
    await tick();
    expect(t.speaker.spoken.slice(2).map((s) => s.lang)).toEqual(['en']);
  });

  it('what the voice could not do reaches the screen instead of passing in silence', async () => {
    // 2026-09-13: "the tts in the chat call is not working" - and the screen said nothing.
    const speaker = new FakeSpeaker();
    let problem: string | null = 'The browser would not play the voice (audio suspended); using the device voice.';
    (speaker as Speaker).takeProblem = () => {
      const p = problem;
      problem = null;
      return p;
    };
    const t = build({ speaker });
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. ');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishOne();
    await tick();
    expect(t.session.state.problem).toContain('would not play the voice');
    // Said once: the second sentence, with nothing wrong, does not repeat it - and the call goes on.
    t.speaker.finishOne();
    await tick();
    expect(t.session.state.phase).toBe('listening');
    expect(t.speaker.spoken).toHaveLength(2);
  });

  it('an answered ring says the result first, sentence by sentence, then listens', async () => {
    const t = build();
    await t.session.start();
    t.session.say('Готово. Отчетът е в Drive.');
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    expect(t.session.state.saying).toEqual(['Готово.', 'Отчетът е в Drive.']);
    t.speaker.finishAll();
    await tick();
    t.speaker.finishAll();
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Готово.', 'Отчетът е в Drive.']);
    expect(t.session.state.phase).toBe('listening');
    expect(t.transport.created).toEqual([]); // saying the result is not a new turn
  });
  it('a pause the turn model is sure about is the end: closed there and sent with no join window', async () => {
    const t = build({ minWaitMs: 5000, patienceMs: 5000, turnThreshold: 0.8 });
    await t.session.start();
    t.transcriber.turn = 0.93;
    t.transcriber.next = 'благодаря, това е всичко';
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['x']), 100);
    await settle();
    // The listener was told to close at that pause, and the words went at once — not after 5 s.
    expect(t.listener.finished).toEqual([100]);
    expect(t.transport.created).toEqual(['благодаря, това е всичко']);
    expect(t.transcriber.calls).toBe(1); // the pause's recognition was the answer
    expect(t.transcriber.opts[0]?.turn).toBe(true);
  });

  it('a pause it hears as unfinished waits for him - "go on" on the screen - then sends what was said', async () => {
    // Until 2026-11-14 a pause it was not sure about changed nothing: the clock sent it after 1.1 s.
    const t = build({ patienceMs: 100, turnThreshold: 0.8 });
    await t.session.start();
    t.transcriber.turn = 0.2;
    t.transcriber.next = 'и освен това';
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['x']), 100);
    await settle();
    expect(t.listener.finished).toEqual([]);
    t.listener.h?.onUtterance(new Blob(['x']), 100, 0, 0);
    await settle();
    expect(t.transport.created).toEqual([]);
    // Waiting, not thinking: listening, the words so far on screen, and 0.6 of 0.8 short of sure → 75 ms.
    expect(t.session.state.phase).toBe('listening');
    expect(t.session.state.heard).toBe('и освен това');
    expect(t.session.state.goOn?.ms).toBe(75);
    await new Promise((r) => setTimeout(r, 110));
    expect(t.transport.created).toEqual(['и освен това']);
    expect(t.session.state.goOn).toBeNull();
    expect(t.listener.turnsSent).toBe(1);
  });

  describe('how long a turn waits, from the turn model\'s score (patience 3 s, sure at 0.9)', () => {
    async function waitFor(turn: number | null, over: Parameters<typeof build>[0] = {}) {
      const t = build({ minWaitMs: 1100, patienceMs: 3000, turnThreshold: 0.9, ...over });
      await t.session.start();
      t.transcriber.turn = turn;
      t.listener.h?.onSpeechStart();
      t.listener.h?.onUtterance(new Blob(['x']), undefined, 0, 0);
      await settle();
      const wait = t.transport.created.length ? 0 : (t.session.state.goOn?.ms ?? -1);
      t.session.end();
      return wait;
    }
    it('sure: none at all, whatever the patience (a quick question is as quick as before)', async () => {
      expect(await waitFor(0.95)).toBe(0);
      expect(await waitFor(0.95, { patienceMs: 8000 })).toBe(0);
    });
    it('a finished sentence scored just under the bar waits no longer than before (1.1 s)', async () => {
      expect(await waitFor(0.87)).toBe(1100);
      expect(await waitFor(0.6)).toBe(1100);
    });
    it('the less finished it sounds, the longer: a cut-off sentence waits nearly the whole patience', async () => {
      expect(await waitFor(0.5)).toBeCloseTo(1333, 0);
      expect(await waitFor(0.1)).toBeCloseTo(2667, 0);
      expect(await waitFor(0.01)).toBeCloseTo(2967, 0);
    });
    it('never longer than the patience: at 1 s everything waits 1 s at most', async () => {
      expect(await waitFor(0.1, { patienceMs: 1000 })).toBe(1000);
      expect(await waitFor(0.8, { patienceMs: 1000 })).toBe(1000);
    });
    it('the model switched off: every pause waits the patience', async () => {
      expect(await waitFor(null, { turnThreshold: null })).toBe(3000);
    });
    it('the model on but unable to say (loading after a deploy): as before, 1.1 s', async () => {
      expect(await waitFor(null)).toBe(1100);
    });
  });

  it('a thinking pause longer than the old 1.5 s cap is one turn: he goes on and all of it is sent once', async () => {
    // 2026-11-14, the defect itself: the timer sent the first half while he talked on, and the rest
    // arrived as steers. Scaled down: a patience of 300 ms, an unsure pause, 350 ms before he finishes.
    const t = build({ patienceMs: 300, turnThreshold: 0.9 });
    await t.session.start();
    t.transcriber.turn = 0.1;
    t.transcriber.next = 'и освен това';
    t.listener.say();
    await new Promise((r) => setTimeout(r, 150)); // thinking
    t.transcriber.next = 'трябва да проверим';
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['x']), 1000);
    await new Promise((r) => setTimeout(r, 200)); // past the patience: words hold the turn
    expect(t.transport.created).toEqual([]);
    expect(t.listener.h?.onPiece?.(1000)).toBe(true);
    t.transcriber.turn = 0.95;
    t.transcriber.next = 'и бюджета.';
    t.listener.h?.onPause(new Blob(['x']), 2000); // sure: finished
    await settle();
    expect(t.transport.created).toEqual(['и освен това трябва да проверим и бюджета.']);
    expect(t.transport.steered).toEqual([]);
    // The words after the thinking pause were recognised with the turn before them as context.
    expect(t.transcriber.opts[1]?.prompt).toBe('и освен това');
  });

  it("utterances join in the order he said them, whichever Whisper answers first", async () => {
    const t = build({ patienceMs: 30 });
    await t.session.start();
    t.transcriber.byAudio = { u1: 'дълго първо изречение', u2: 'и кратко' };
    t.transcriber.delayByAudio = { u1: 60 };
    t.listener.h?.onSpeechStart();
    t.listener.h?.onUtterance(new Blob(['u1']), undefined, 0, 0);
    t.listener.h?.onSpeechStart();
    t.listener.h?.onUtterance(new Blob(['u2']), undefined, 0, 0);
    await new Promise((r) => setTimeout(r, 130));
    expect(t.transport.created).toEqual(['дълго първо изречение и кратко']);
  });

  it('a piece Whisper fails on is not the whole turn: what came back is kept, the problem said once', async () => {
    const t = build({ patienceMs: 30 });
    await t.session.start();
    t.transcriber.byAudio = { p1: 'първо', p2: 'второ', tail: 'трето' };
    t.transcriber.failAudio = ['p2'];
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['p1']), 100);
    await tick();
    t.listener.h?.onPiece?.(100);
    t.listener.h?.onPause(new Blob(['p2']), 200);
    await tick();
    t.listener.h?.onPiece?.(200);
    t.listener.h?.onUtterance(new Blob(['tail']), 300, 0, 0);
    await new Promise((r) => setTimeout(r, 60));
    expect(t.transport.created).toEqual(['първо трето']);
    const shown = t.states.map((s) => s.problem).filter((p, i, all) => p && p !== all[i - 1]);
    expect(shown).toHaveLength(1);
    expect(shown[0]).toMatch(/can't hear you/);
  });

  it('nothing is read out over his pause: a ring result and an answer wait until his turn is sent', async () => {
    const t = build({ patienceMs: 80 });
    await t.session.start();
    t.transcriber.next = 'провери пощата';
    t.listener.say();
    await new Promise((r) => setTimeout(r, 100));
    t.session.onRunQueued('r1', 'c1');
    t.transcriber.next = 'и после';
    t.listener.say(); // he goes on while it works: held, waiting for the rest
    await settle();
    t.session.onDelta('r1', 'Пощата: три нови писма.');
    t.session.onStepDone('r1', false); // an answer to the first half
    t.session.onRunDone('r1');
    t.session.say('Напомняне: среща в 10.');
    await tick();
    expect(t.speaker.spoken).toEqual([]);
    expect(t.session.state.goOn).not.toBeNull();
    await new Promise((r) => setTimeout(r, 100));
    // Sent: the run had finished, so it is a new turn - and what waited is said now, in order.
    expect(t.transport.created).toEqual(['провери пощата', 'и после']);
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Пощата: три нови писма.']);
  });

  it('muted mid-sentence: what he said is sent at once, not thrown away', async () => {
    const t = build({ patienceMs: 5000 });
    await t.session.start();
    t.transcriber.byAudio = { p1: 'първо', open: 'второ' };
    t.listener.open = true;
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['p1']), 100);
    await tick();
    t.listener.h?.onPiece?.(100);
    t.session.setMuted(true);
    await settle();
    expect(t.transport.created).toEqual(['първо второ']);
  });

  it('a route change mid-sentence keeps what was heard, and the clock waits for the new microphone', async () => {
    const t = build({ patienceMs: 40 });
    await t.session.start();
    t.transcriber.byAudio = { p1: 'първо', open: 'второ' };
    t.listener.open = true;
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['p1']), 100);
    await tick();
    t.listener.h?.onPiece?.(100);
    let reopen: () => void = () => undefined;
    t.listener.routeGate = new Promise((r) => (reopen = r));
    const switching = t.session.setRoute('speaker');
    await new Promise((r) => setTimeout(r, 70));
    expect(t.transport.created).toEqual([]); // he cannot be heard yet: nothing is decided for him
    reopen();
    await switching;
    await new Promise((r) => setTimeout(r, 70));
    expect(t.transport.created).toEqual(['първо второ']);
  });

  it('a "sure" turn is not sent while he has already started the next sentence: its first words decide', async () => {
    // Alpha 2026-11-14 (F-01): the decision came 0.7-1.1 s after the utterance closed, he was 30-491 ms
    // into the next sentence, and only a confirmed attack held the send - 16 sends for one briefing.
    const t = build({ turnThreshold: 0.9, patienceMs: 3000 });
    await t.session.start();
    t.transcriber.turn = 0.97;
    t.transcriber.delayMs = 30;
    t.transcriber.next = 'Първо обсъдихме графика.';
    t.listener.h?.onSpeechStart();
    t.listener.h?.onUtterance(new Blob(['x']), undefined, 0, 500);
    t.listener.onset(); // the next sentence begins while Whisper works
    await new Promise((r) => setTimeout(r, 60));
    expect(t.transport.created).toEqual([]); // "sure", but he is talking
    t.transcriber.delayMs = 0;
    t.transcriber.turn = 0.2;
    t.transcriber.next = 'След това бюджета';
    t.listener.h?.onSpeechStart(); // the onset was speech after all
    t.listener.h?.onPause(new Blob(['x']), 2000);
    await settle();
    expect(t.transport.created).toEqual([]); // words: held
    t.transcriber.turn = 0.96;
    t.transcriber.next = 'и разходите.';
    t.listener.h?.onUtterance(new Blob(['y']), 3000, 0, 500);
    await settle();
    expect(t.transport.created).toEqual(['Първо обсъдихме графика. и разходите.']);
    expect(t.transport.steered).toEqual([]);
  });

  it('a sound that never becomes speech holds a ready turn only until it is quiet', async () => {
    const t = build({ turnThreshold: 0.9 });
    await t.session.start();
    t.transcriber.turn = 0.97;
    t.listener.h?.onSpeechStart();
    t.listener.h?.onUtterance(new Blob(['x']), undefined, 0, 500);
    t.listener.onset(); // a click before the recognition is back
    await settle();
    expect(t.transport.created).toEqual([]);
    t.listener.quiet(); // it was nothing
    expect(t.transport.created).toEqual(['какво ще кажеш']);
  });

  it('the patience does not run out under him: sound before it ends stops the clock (US-1, 2.5 s pause)', async () => {
    // Alpha 2026-11-14: at patience 3 s and a 2.5 s pause the clock ran out 140 ms after he went on.
    const t = build({ patienceMs: 80 });
    await t.session.start();
    t.transcriber.next = 'и освен това';
    t.listener.say();
    await new Promise((r) => setTimeout(r, 50));
    t.listener.onset(); // he goes on, not yet confirmed as speech
    await new Promise((r) => setTimeout(r, 80)); // past the patience
    expect(t.transport.created).toEqual([]);
    t.listener.quiet(); // only a breath: the 30 ms that were left run now
    expect(t.transport.created).toEqual([]);
    await new Promise((r) => setTimeout(r, 60));
    expect(t.transport.created).toEqual(['и освен това']);
  });

  it("a piece that is back is the next piece's prompt at once, at the same pause", async () => {
    // Alpha 2026-11-14 (F-03): the listener closes a piece and asks for the next recognition in one
    // go; the piece's words reached the prompt a microtask later, so every prompt missed one.
    const t = build();
    await t.session.start();
    t.transcriber.byAudio = { p1: 'Защото цените са се вдигнали.', p2: 'Петър поиска отчет.' };
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['p1']), 100);
    await tick();
    t.listener.h?.onPiece?.(100);
    t.listener.h?.onPause(new Blob(['p2']), 200);
    expect(t.transcriber.opts.at(-1)?.prompt).toBe('Защото цените са се вдигнали.');
  });

  it("hanging up mid-turn is in the call's trace, before the trace stops with the call", async () => {
    // Alpha 2026-11-14 (F-06): the mark was written once Whisper was back, after the trace had stopped.
    const marks: string[] = [];
    const spy = vi.spyOn(callTrace, 'mark').mockImplementation((k: string, data?: Record<string, unknown>) => {
      if (k === 'turn.send') marks.push(String(data?.why));
    });
    try {
      const t = build({ patienceMs: 5000 });
      await t.session.start();
      t.listener.say();
      await settle();
      t.session.end();
      expect(marks).toEqual(['hangup']);
      await settle();
      expect(t.transport.created).toEqual(['какво ще кажеш']);
    } finally {
      spy.mockRestore();
    }
  });

  it('Jarvis does not start speaking while he is making a sound; a sound that was nothing lets him', async () => {
    // Alpha round 2 (2026-11-15): Jarvis began his answer 50 ms after Arsen began to speak in thinking.
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.listener.onset(); // he starts to say something while the answer is written
    t.session.onDelta('r1', 'Готово, записах го.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.speaker.spoken).toEqual([]);
    t.listener.quiet(); // it was nothing: the answer is said
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Готово, записах го.']);
  });

  describe('a sound while Jarvis works', () => {
    // 2026-11-24, a real call: a noise at 31 s, Whisper found no words, and the screen said
    // "Listening" without the music for the 57 s the run still worked.
    async function working() {
      const t = build();
      await t.session.start();
      t.listener.say();
      await settle();
      t.session.onRunQueued('r1', 'c1');
      expect(t.session.state.phase).toBe('thinking');
      t.cues.log.length = 0;
      t.states.length = 0;
      return t;
    }

    it('with no words in it changes nothing: the music plays on, nothing is sent', async () => {
      const t = await working();
      t.transcriber.next = '';
      t.listener.say();
      await settle();
      expect(new Set(t.phases())).toEqual(new Set(['thinking']));
      expect(t.cues.log).toEqual([]); // not stopped, not started again
      expect(t.transport.steered).toEqual([]);
      t.session.onDelta('r1', 'Готово.');
      t.session.onStepDone('r1', false);
      t.session.onRunDone('r1');
      await tick();
      expect(t.session.state.phase).toBe('speaking'); // and the answer is said as ever
    });

    it('a cough the listener drops, or Whisper failing, leaves the music on too', async () => {
      const t = await working();
      t.listener.cough();
      t.transcriber.fail = true;
      t.listener.say();
      await settle();
      expect(new Set(t.phases())).toEqual(new Set(['thinking']));
      expect(t.cues.log).toEqual([]);
    });

    it('with words in it is him: the music stops and the words reach the run', async () => {
      const t = await working();
      t.transcriber.next = 'чакай, и утре';
      t.listener.say();
      await settle();
      expect(t.cues.log[0]).toBe('thinking:false');
      expect(t.phases()).toContain('listening');
      expect(t.transport.steered).toEqual([{ runId: 'r1', text: 'чакай, и утре' }]);
    });

    it('is listened to at the first pause that has words, not only at the end of the sentence', async () => {
      const t = await working();
      t.transcriber.next = 'чакай';
      t.listener.h?.onSpeechStart();
      expect(t.session.state.phase).toBe('thinking'); // a sound, not yet words
      t.listener.h?.onPause(new Blob(['x']), 100);
      await tick();
      expect(t.session.state.phase).toBe('listening');
      expect(t.cues.log).toEqual(['thinking:false']);
      t.listener.h?.onSpeechDropped(); // came to nothing after all: back to the music
      expect(t.session.state.phase).toBe('thinking');
    });
  });

  it('words said while the answer is written reach the run, and that answer is not read out', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.listener.h?.onSpeechStart(); // "Чакай, спри за малко." while it thinks
    t.session.onDelta('r1', 'Римската империя започва през 753 г. пр.н.е.');
    t.session.onStepDone('r1', false);
    await tick();
    expect(t.speaker.spoken).toEqual([]);
    t.transcriber.next = 'Чакай, спри за малко.';
    t.listener.h?.onUtterance(new Blob(['x']), undefined, 0, 500);
    await settle();
    expect(t.transport.steered.map((s) => s.text)).toEqual(['Чакай, спри за малко.']);
    expect(t.speaker.spoken).toEqual([]); // the answer to the first half is held back
    t.session.onDelta('r1', 'Спрях.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Спрях.']);
  });

  it('a turn held by words that never end is sent at the ceiling; nothing waits for ever', async () => {
    const t = build({ patienceMs: 5000, ceilingMs: 60 });
    await t.session.start();
    t.listener.say();
    await settle();
    t.listener.h?.onSpeechStart(); // a television: Whisper finds words in it
    t.listener.h?.onPause(new Blob(['x']), 100);
    await new Promise((r) => setTimeout(r, 90));
    expect(t.transport.created).toEqual(['какво ще кажеш']);
  });

  it('without the turn model (switched off) nothing is asked of it', async () => {
    const t = build({ turnThreshold: null });
    await t.session.start();
    t.listener.say();
    await settle();
    expect(t.transcriber.opts[0]?.turn).toBe(false);
    expect(t.transport.created).toHaveLength(1);
  });

  it('a long utterance arrives in pieces: each piece is recognised once, the rest after it, all in order', async () => {
    const t = build();
    await t.session.start();
    t.transcriber.byAudio = { p1: 'Добре, и накрая,', p2: 'като свършиш всичко,', tail: 'обади ми се.' };
    t.listener.h?.onSpeechStart();
    const seconds = () => new Promise((r) => setTimeout(r, 10)); // pieces are seconds apart
    t.listener.h?.onPause(new Blob(['p1']), 100);
    await seconds();
    // He goes on: the first part is a piece now.
    expect(t.listener.h?.onPiece?.(100)).toBe(true);
    await seconds();
    t.listener.h?.onPause(new Blob(['p2']), 200);
    await seconds();
    expect(t.listener.h?.onPiece?.(200)).toBe(true);
    await seconds();
    // A pause no recognition was started for cannot become a piece.
    expect(t.listener.h?.onPiece?.(250)).toBe(false);
    t.listener.h?.onUtterance(new Blob(['tail']), 300, 4000);
    await settle();
    expect(t.transport.created).toEqual(['Добре, и накрая, като свършиш всичко, обади ми се.']);
    expect(t.transcriber.calls).toBe(3);
    // Each piece was given the words before it as Whisper's prompt.
    expect(t.transcriber.opts[1]?.prompt).toBe('Добре, и накрая,');
    expect(t.transcriber.opts[2]?.prompt).toBe('Добре, и накрая, като свършиш всичко,');
    expect(t.transcriber.opts[2]?.offsetMs).toBe(4000);
  });

  it('what Jarvis last said is not Whisper\'s prompt: a prompt in another language makes it invent words', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'One thing. Are you waiting on me?');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishAll();
    await tick();
    t.speaker.finishAll();
    await tick();
    t.transcriber.next = 'не съм';
    t.listener.say();
    await settle();
    expect(t.transcriber.opts.at(-1)?.prompt).toBeUndefined();
  });

  it('only the first sentence of a reply is asked for as the first', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето. ');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    // Asked for twice (fetched while written, then queued) - the same sentence both times.
    expect([...new Set(t.speaker.firsts)]).toEqual(['Първо.']);
  });

  it('talking on while the answer is written: that answer is not read out, the one with all of it is', async () => {
    const t = build();
    await t.session.start();
    t.transcriber.next = 'провери пощата';
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Пощата: три нови писма. ');
    // He goes on before that step is over.
    t.transcriber.next = 'и ми кажи времето';
    t.listener.say();
    await settle();
    expect(t.transport.steered.map((s) => s.text)).toEqual(['и ми кажи времето']);
    t.session.onStepDone('r1', false); // the answer to the first half only
    await tick();
    expect(t.speaker.spoken).toEqual([]);
    // The core goes on with his words and answers both at once: that is what he hears.
    t.session.onDelta('r1', 'Три нови писма, и е слънчево, 18 градуса.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishAll();
    await tick();
    expect(t.speaker.spoken.map((x) => x.text).join(' ')).toBe('Три нови писма, и е слънчево, 18 градуса.');
  });

  it('if the core finished instead (his words came too late), the held answer is said after all', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Пощата: три нови писма. ');
    t.transcriber.next = 'и времето';
    t.listener.say();
    await settle();
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1'); // no second step: the steer becomes a run of its own
    await tick();
    expect(t.speaker.spoken.map((x) => x.text)).toEqual(['Пощата: три нови писма.']);
  });

  it('words added while the run is calling tools are simply part of the next step', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.transcriber.next = 'и времето';
    t.listener.say();
    await settle();
    t.session.onStepDone('r1', true); // a tool step: his words reach the next one
    t.session.onDelta('r1', 'Три писма и слънце.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.speaker.spoken.map((x) => x.text)).toEqual(['Три писма и слънце.']);
  });


  it('hung up while Whisper works: what he said is sent once it is back, and the call stays ended', async () => {
    // Gate B, 2026-11-14: hanging up is how a briefing ends; until then its last words were dropped.
    const transcriber = new FakeTranscriber();
    transcriber.delayMs = 20;
    const t = build({ transcriber });
    await t.session.start();
    t.listener.say();
    t.session.end();
    const changes = t.states.length;
    await new Promise((r) => setTimeout(r, 40));
    expect(t.transport.created).toEqual(['какво ще кажеш']);
    expect(t.session.state.phase).toBe('ended');
    expect(t.states.length).toBe(changes); // nothing on a screen that is gone
    expect(t.speaker.spoken).toEqual([]);
  });

  it('hung up while a turn waits for him: what is held, and the pieces still with Whisper, are sent in order', async () => {
    const t = build({ patienceMs: 5000 });
    await t.session.start();
    t.transcriber.byAudio = { x: 'първо', p1: 'второ' };
    t.transcriber.delayByAudio = { p1: 20 };
    t.listener.say();
    await settle();
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['p1']), 100);
    t.listener.h?.onPiece?.(100);
    t.session.end();
    await new Promise((r) => setTimeout(r, 40));
    expect(t.transport.created).toEqual(['първо второ']);
  });

  it('hung up and Whisper is not back within the wait: what is held is sent, the rest dropped', async () => {
    const t = build({ patienceMs: 5000, hangUpMs: 20 });
    await t.session.start();
    t.transcriber.byAudio = { x: 'първо', p1: 'второ' };
    t.transcriber.delayByAudio = { p1: 80 };
    t.listener.say();
    await settle();
    t.listener.h?.onSpeechStart();
    t.listener.h?.onPause(new Blob(['p1']), 100);
    t.listener.h?.onPiece?.(100);
    t.session.end();
    await new Promise((r) => setTimeout(r, 120));
    expect(t.transport.created).toEqual(['първо']);
  });

  it('hung up while the microphone is being asked for: the call stays ended', async () => {
    const listener = new FakeListener();
    let grant: () => void = () => undefined;
    listener.start = (h) => {
      listener.h = h;
      return new Promise((r) => (grant = r));
    };
    const t = build({ listener });
    const starting = t.session.start();
    t.session.end();
    grant();
    await starting;
    expect(t.session.state.phase).toBe('ended');
  });

  it('a failed run says so with the microphone gated, after the cut answer has unwound', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ.');
    t.session.onStepDone('r1', false);
    await tick();
    expect(t.listener.gated).toBe(true);
    t.session.onRunFailed('r1', 'boom');
    await tick();
    await tick();
    // The old answer's ending must not leave the microphone open while the problem is said.
    expect(t.speaker.spoken.at(-1)?.text).toContain('boom');
    expect(t.listener.gated).toBe(true);
    t.speaker.finishAll();
    await tick();
    expect(t.listener.gated).toBe(false);
    expect(t.session.state.phase).toBe('listening');
  });

  it('a long reply has only the next two sentences synthesised ahead, not all of it at once', async () => {
    // All at once filled the core's two synthesis slots; after a cut-in the new answer waited
    // behind sentences nobody would hear.
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onStepDone('r1', false);
    t.speaker.prepared = [];
    t.session.say('Едно. Две. Три. Четири. Пет.');
    await tick();
    // "Едно." is playing; "Две." and "Три." are ready.
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Едно.']);
    expect(new Set(t.speaker.prepared)).toEqual(new Set(['Едно.', 'Две.', 'Три.']));
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.prepared).toContain('Четири.');
    expect(t.speaker.prepared).not.toContain('Пет.');
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.prepared).toContain('Пет.');
  });

  it('a sentence fetched while the step is written is fetched in the language it will be said in', async () => {
    // Fetched in its own script's language, an English sentence in a Bulgarian-voiced reply was
    // synthesised twice (2026-10-15).
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Проверих пощата. Nothing new today. Това е.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishAll();
    await tick();
    t.speaker.finishAll();
    await tick();
    const said = t.speaker.spoken.map((s) => `${s.lang}|${s.text}`);
    expect(said).toContain('bg|Nothing new today.');
    // Every sentence said was prepared under the key it is said with (the server voice
    // ignores a key it already has), and never under another.
    for (const key of said) expect(t.speaker.preparedKeys).toContain(key);
    expect(t.speaker.preparedKeys).not.toContain('en|Nothing new today.');
  });
});

/** A voice that can hold mid-sentence (the line, voice/line.ts). */
class PausingSpeaker extends FakeSpeaker {
  log: string[] = [];
  pause(): void {
    this.log.push('pause');
  }
  resume(): void {
    this.log.push('resume');
  }
}

describe('a sound over a voice that can pause', () => {
  async function speakingThree() {
    const speaker = new PausingSpeaker();
    const t = build({ speaker });
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето.');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    return { ...t, speaker };
  }

  it('a sound with no words in it (a cheek on the glass) holds him and lets him go on mid-word', async () => {
    const t = await speakingThree();
    t.transcriber.next = ''; // Whisper: not speech
    t.listener.say();
    expect(t.speaker.log).toEqual(['pause']);
    expect(t.speaker.cancelled).toBe(0);
    await settle();
    expect(t.speaker.log).toEqual(['pause', 'resume']);
    expect(t.session.state.phase).toBe('speaking');
    for (let i = 0; i < 3; i += 1) {
      t.speaker.finishOne();
      await tick();
    }
    // Nothing said twice, nothing cut, nothing sent.
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Първо.', 'Второ.', 'Трето.']);
    expect(t.speaker.cancelled).toBe(0);
    expect(t.transport.created).toHaveLength(1);
  });

  it('a cough the listener drops resumes him too', async () => {
    const t = await speakingThree();
    t.listener.cough();
    expect(t.speaker.log).toEqual(['pause', 'resume']);
    expect(t.speaker.cancelled).toBe(0);
  });

  it('words over him cut him for good, and the run hears he was cut off', async () => {
    const t = await speakingThree();
    t.transcriber.next = 'чакай';
    t.listener.say();
    expect(t.speaker.cancelled).toBe(0); // only paused while Whisper listens
    await settle();
    expect(t.speaker.cancelled).toBe(1);
    t.speaker.finishAll();
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Първо.']);
    expect(t.transport.created).toHaveLength(2);
    expect(t.transport.created[1]).toMatch(/cut you off/);
    expect(t.transport.created[1]).toContain('чакай');
  });
});
