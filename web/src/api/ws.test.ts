import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SILENCE_LIMIT, WsClient } from './ws';

/** A socket that opens when told and otherwise stays silent - the zombie a slept phone keeps. */
class FakeSocket {
  static all: FakeSocket[] = [];
  static OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true; // a zombie never fires onclose
  }
  accept() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
}

describe('WsClient liveness', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.stubGlobal('window', {
      location: { protocol: 'http:', host: 'core', href: 'http://core/' },
      history: { replaceState: () => undefined },
    });
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => undefined,
    });
    vi.stubGlobal('document', { visibilityState: 'hidden' });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function client() {
    const opens: boolean[] = [];
    const c = new WsClient({
      onEvents: () => undefined,
      onState: () => undefined,
      onOpen: (re) => opens.push(re),
    });
    c.connect();
    FakeSocket.all[0].accept();
    return { c, opens };
  }

  it('reopens a socket that stays open but silent, and the reopen counts as a reconnect', () => {
    const { opens } = client();
    vi.advanceTimersByTime(SILENCE_LIMIT + 30_000);
    expect(FakeSocket.all[0].closed).toBe(true);
    expect(FakeSocket.all).toHaveLength(2);
    FakeSocket.all[1].accept();
    expect(opens).toEqual([false, true]); // the store refetches the conversation list on `true`
  });

  it('keeps a socket that keeps answering', () => {
    client();
    for (let i = 0; i < 6; i++) {
      vi.advanceTimersByTime(25_000);
      FakeSocket.all[0].onmessage?.({ data: '{"type":"pong"}' });
    }
    expect(FakeSocket.all).toHaveLength(1);
    expect(
      FakeSocket.all[0].sent.filter((s) => s.includes('ping')),
    ).toHaveLength(6);
  });

  it('revive() drops a silent socket at once when the page comes back', () => {
    const { c } = client();
    vi.advanceTimersByTime(20_000);
    c.revive(); // heard from 20 s ago: fine
    expect(FakeSocket.all).toHaveLength(1);
    vi.setSystemTime(Date.now() + 60_000); // the phone slept: no timers ran
    c.revive();
    expect(FakeSocket.all[0].closed).toBe(true);
    expect(FakeSocket.all).toHaveLength(2);
  });
});
