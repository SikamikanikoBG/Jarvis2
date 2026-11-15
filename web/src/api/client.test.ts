import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';

describe('api.stt', () => {
  afterEach(() => vi.unstubAllGlobals());

  async function sent(opts: Parameters<typeof api.stt>[2]): Promise<FormData> {
    let body: FormData | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn((_path: string, init: { body: FormData }) => {
        body = init.body;
        return Promise.resolve(new Response(JSON.stringify({ text: '', language: 'bg' }), { status: 200 }));
      }),
    );
    await api.stt(new Blob(['x'], { type: 'audio/wav' }), 'bg', opts);
    if (!body) throw new Error('nothing sent');
    return body;
  }

  it("an incognito call's clip is sent with keep=false, so voice_debug does not keep it (EC-11)", async () => {
    expect((await sent({ keep: false })).get('keep')).toBe('false');
    // Any other call says nothing: the core keeps it as before when voice_debug is on.
    expect((await sent({ keep: true })).get('keep')).toBeNull();
    expect((await sent({})).get('keep')).toBeNull();
  });
});
