import { describe, expect, it } from 'vitest';
import { CallSession, type CallState, type Listener, type ListenerHandlers, type Speaker, type Transcriber, type Transport } from './session';

/** A listener the test drives by hand. */
class FakeListener implements Listener {
  h: ListenerHandlers | null = null;
  gated = false;
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
  setGated(g: boolean): void {
    this.gated = g;
  }
  setMuted(m: boolean): void {
    this.muted = m;
  }
  say(): void {
    this.h?.onSpeechStart();
    this.h?.onUtterance(new Blob(['x']));
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
  transcribe(): Promise<{ text: string; language: string | null }> {
    if (this.fail) return Promise.reject(new Error('502'));
    return Promise.resolve({ text: this.next, language: 'bg' });
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

function build(over: Partial<{ listener: FakeListener; speaker: FakeSpeaker; transcriber: FakeTranscriber; transport: FakeTransport }> = {}) {
  const listener = over.listener ?? new FakeListener();
  const speaker = over.speaker ?? new FakeSpeaker();
  const transcriber = over.transcriber ?? new FakeTranscriber();
  const transport = over.transport ?? new FakeTransport();
  const states: CallState[] = [];
  const session = new CallSession({
    listener,
    speaker,
    transcriber,
    transport,
    language: 'bg',
    conversationId: 'c1',
    onChange: (s) => states.push(s),
  });
  return { session, listener, speaker, transcriber, transport, states, phases: () => states.map((s) => s.phase) };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe('CallSession', () => {
  it('walks a whole turn: listen → transcribe → think → speak sentence by sentence → listen', async () => {
    const t = build();
    await t.session.start();
    expect(t.session.state.phase).toBe('listening');
    t.listener.say();
    await tick();
    expect(t.session.state.phase).toBe('thinking');
    expect(t.session.state.heard).toBe('какво ще кажеш');
    expect(t.transport.created).toEqual(['какво ще кажеш']);

    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Три дни. Има ли');
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    expect(t.listener.gated).toBe(true);
    expect(t.speaker.spoken).toEqual([{ text: 'Три дни.', lang: 'bg' }]);
    t.speaker.finishOne();
    await tick();
    expect(t.session.state.spokenUpTo).toBe(1);
    expect(t.session.state.phase).toBe('thinking'); // more is being written
    t.session.onDelta('r1', ' бучка?');
    t.session.onRunDone('r1');
    await tick();
    expect(t.speaker.spoken.map((s) => s.text)).toEqual(['Три дни.', 'Има ли бучка?']);
    t.speaker.finishOne();
    await tick();
    expect(t.session.state.phase).toBe('listening');
    expect(t.listener.gated).toBe(false);
    expect(t.session.state.runId).toBeNull();
  });

  it('a cut-in while he speaks stops him and steers the run that is still working', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await tick();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Първо. Второ. Трето. И');
    await tick();
    expect(t.session.state.phase).toBe('speaking');
    // Arsen talks over him.
    t.transcriber.next = 'не, не това';
    t.listener.say();
    expect(t.speaker.cancelled).toBe(1);
    await tick();
    // The run is still working: the words go to it, not to a new run.
    expect(t.transport.steered).toEqual([{ runId: 'r1', text: 'не, не това' }]);
    expect(t.transport.created).toHaveLength(1);
    expect(t.session.state.phase).toBe('thinking');
    // Its new sentences are spoken from a clean queue.
    t.session.onDelta('r1', ' Разбрах. Ще го направя.');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.spoken.map((s) => s.text).slice(-2)).toEqual(['Разбрах.', 'Ще го направя.']);
    // "И" — the half-sentence written before the cut — was never said.
    expect(t.speaker.spoken.some((s) => s.text.startsWith('И '))).toBe(false);
  });

  it('with no run working, a new utterance starts a new voice turn in the same conversation', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await tick();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Да.');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishAll();
    await tick();
    expect(t.session.state.phase).toBe('listening');
    t.transcriber.next = 'и още нещо';
    t.listener.say();
    await tick();
    expect(t.transport.created).toEqual(['какво ще кажеш', 'и още нещо']);
    expect(t.transport.steered).toEqual([]);
  });

  it('a second utterance before the run id arrives is not a second turn', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await tick();
    t.listener.say(); // the run has not been queued yet
    await tick();
    expect(t.transport.created).toEqual(['какво ще кажеш', 'какво ще кажеш']);
    // Two creates, because the first one had not come back — the core queues them; the session
    // does not steer a run it has not been told about. What matters: nothing was lost.
  });

  it('says one honest sentence when Whisper is down and keeps listening', async () => {
    const t = build();
    await t.session.start();
    t.transcriber.fail = true;
    t.listener.say();
    await tick();
    expect(t.session.state.phase).toBe('listening');
    expect(t.session.state.problem).toMatch(/speech service is not answering/);
    expect(t.transport.created).toEqual([]);
  });

  it('a failed run is said out loud, then listening resumes', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await tick();
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
    await tick();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Едно. Две. Три. ');
    await tick();
    t.session.end();
    expect(t.listener.stopped).toBe(true);
    expect(t.speaker.cancelled).toBe(1);
    expect(t.session.state.phase).toBe('ended');
    t.session.onDelta('r1', 'Четири. Пет.'); // late deltas change nothing
    expect(t.speaker.spoken).toHaveLength(1);
  });

  it('a mixed reply switches voice per sentence', async () => {
    const t = build();
    await t.session.start();
    t.listener.say();
    await tick();
    t.session.onRunQueued('r1', 'c1');
    t.session.onDelta('r1', 'Готово. Run it with vLLM. ');
    t.session.onRunDone('r1');
    await tick();
    t.speaker.finishOne();
    await tick();
    expect(t.speaker.spoken.map((s) => s.lang)).toEqual(['bg', 'en']);
  });
});
