import { describe, expect, it, vi } from 'vitest';
import { CallTrace, type TraceEvent } from './trace';

const make = () => {
  const sent: { id: string; events: TraceEvent[] }[] = [];
  let now = 0;
  const trace = new CallTrace(
    (id, events) => {
      sent.push({ id, events });
      return Promise.resolve();
    },
    () => now,
  );
  return { trace, sent, tick: (ms: number) => (now += ms) };
};

describe('CallTrace', () => {
  it('records nothing until a call starts', () => {
    const { trace, sent } = make();
    trace.mark('ear', { on: true });
    trace.stop();
    expect(sent).toEqual([]);
  });

  it('stamps events from the start of the call and sends them all at the end', () => {
    const { trace, sent, tick } = make();
    trace.start({ line: true });
    tick(1200);
    trace.mark('pause');
    trace.stop();
    const kinds = sent.flatMap((b) => b.events.map((e) => [e.k, e.t]));
    expect(kinds).toEqual([['start', 0], ['pause', 1200], ['end', 1200]]);
    expect(sent[0]?.id).toMatch(/^\d{8}-\d{6}-[a-z0-9]+$/);
  });

  it('a streaming channel keeps one reading per interval', () => {
    const { trace, sent, tick } = make();
    trace.start({});
    for (let i = 0; i < 10; i++) {
      trace.sample('g', 100, { y: i });
      tick(30);
    }
    trace.stop();
    expect(sent.flatMap((b) => b.events).filter((e) => e.k === 'g').map((e) => e.y)).toEqual([0, 4, 8]);
  });

  it('reads the line stats every second and flushes as it goes', () => {
    vi.useFakeTimers();
    try {
      const { trace, sent } = make();
      trace.start({});
      trace.watchStats(() => Promise.resolve({ level: 0.2 }));
      vi.advanceTimersByTime(3000);
      return Promise.resolve().then(() => {
        expect(sent.length).toBeGreaterThan(0);
        trace.stop();
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
