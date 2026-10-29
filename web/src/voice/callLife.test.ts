import { describe, expect, it } from 'vitest';
import { CALL_ALIVE_MS, CallLife, type CallLifeDeps } from './callLife';

function harness(open = true) {
  const sent: unknown[] = [];
  const beacons: string[] = [];
  let tick: (() => void) | null = null;
  let interval: number | null = null;
  const state = { open };
  const deps: CallLifeDeps = {
    send: (m) => {
      if (!state.open) return false;
      sent.push(m);
      return true;
    },
    beacon: (id) => beacons.push(id),
    newId: () => 'call_1',
    setInterval: (fn, ms) => {
      tick = fn;
      interval = ms;
      return 7;
    },
    clearInterval: () => {
      tick = null;
    },
  };
  return { sent, beacons, deps, state, tick: () => tick?.(), interval: () => interval, ticking: () => tick !== null };
}

describe('the call as the core sees it', () => {
  it('says it is alive at once and every 20 s, with its chat', () => {
    const h = harness();
    const life = new CallLife(h.deps, 'conv_a');
    life.start();
    expect(h.interval()).toBe(CALL_ALIVE_MS);
    h.tick();
    life.setConversation('conv_b');
    h.tick();
    expect(h.sent).toEqual([
      { type: 'call.alive', call_id: 'call_1', conversation_id: 'conv_a' },
      { type: 'call.alive', call_id: 'call_1', conversation_id: 'conv_a' },
      { type: 'call.alive', call_id: 'call_1', conversation_id: 'conv_b' },
    ]);
  });

  it('hangs up exactly once, however many ways it is asked to', () => {
    const h = harness();
    const life = new CallLife(h.deps, 'conv_a');
    life.start();
    life.end();
    life.end();
    life.end(true); // the page going away after the red button
    expect(h.sent.filter((m) => (m as { type: string }).type === 'call.ended')).toEqual([{ type: 'call.ended', call_id: 'call_1' }]);
    expect(h.beacons).toEqual([]);
    expect(h.ticking()).toBe(false);
    life.alive();
    expect(h.sent.filter((m) => (m as { type: string }).type === 'call.alive')).toHaveLength(1);
  });

  it('a page going away hangs up by keepalive request, and so does a closed socket', () => {
    const away = harness();
    new CallLife(away.deps, null).end(true);
    expect(away.beacons).toEqual(['call_1']);
    const closed = harness(false);
    const life = new CallLife(closed.deps, null);
    life.end();
    expect(closed.beacons).toEqual(['call_1']);
    expect(life.isEnded).toBe(true);
  });
});
