import { describe, expect, it } from 'vitest';
import { CallSession, type CallState, type Listener, type ListenerHandlers, type Speaker, type Transcriber, type Transport } from './session';
import type { Cues } from './tones';

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
  setRoute(r: 'earpiece' | 'speaker'): void {
    this.route = r;
  }
  setMuted(m: boolean): void {
    this.muted = m;
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
}

/** A speaker that finishes when the test says so. */
class FakeSpeaker implements Speaker {
  spoken: { text: string; lang: string }[] = [];
  cancelled = 0;
  private pending: (() => void)[] = [];
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
  transcribe(_audio: Blob, language: string): Promise<{ text: string; language: string | null }> {
    this.hints.push(language);
    this.calls += 1;
    if (this.fail) return Promise.reject(new Error('502'));
    const res = { text: this.next, language: this.language };
    return this.delayMs ? new Promise((r) => setTimeout(() => r(res), this.delayMs)) : Promise.resolve(res);
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
  over: Partial<{ listener: FakeListener; speaker: FakeSpeaker; transcriber: FakeTranscriber; transport: FakeTransport; joinMs: number }> = {},
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
    joinMs: over.joinMs ?? 0,
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

  it('a step that ends in tool calls is not read out: its words were the plan, not the reply', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await settle();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'The user is on a voice call. Let me look that up. ');
    t.session.onStepDone('r1', true);
    await tick();
    expect(t.speaker.spoken).toEqual([]);
    expect(t.session.state.phase).toBe('thinking');
    t.session.onDelta('r1', 'Три дни. ');
    t.session.onStepDone('r1', false);
    t.session.onRunDone('r1');
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Три дни.']);
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
    const t = build({ joinMs: 30 });
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

  it('the join window counts from the end of speech, not from when the recogniser answered', async () => {
    const t = build({ joinMs: 80 });
    t.transcriber.delayMs = 60;
    await t.session.start();
    const t0 = Date.now();
    t.listener.say();
    await new Promise((r) => setTimeout(r, 105));
    // 60 ms of recognition + 80 ms of window would be 140; from the end of speech it is 80.
    expect(t.transport.created).toEqual(['какво ще кажеш']);
    expect(Date.now() - t0).toBeLessThan(140);
  });

  it('a noisy room does not hold his words: only recognised text extends the wait', async () => {
    const t = build({ joinMs: 30 });
    await t.session.start();
    t.listener.say();
    await tick();
    for (let i = 0; i < 5; i += 1) {
      t.listener.h?.onSpeechStart(); // something in the room, every few ms, never words
      await new Promise((r) => setTimeout(r, 10));
    }
    await new Promise((r) => setTimeout(r, 30));
    expect(t.transport.created).toEqual(['какво ще кажеш']);
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
    t.session.setRoute('speaker');
    expect(t.listener.route).toBe('speaker');
    expect(t.session.state.route).toBe('speaker');
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
});
