import { afterEach, describe, expect, it, vi } from 'vitest';
import { takeRing } from './router';

function at(href: string) {
  const url = new URL(href);
  const replaced: string[] = [];
  vi.stubGlobal('window', {
    location: { href, origin: url.origin, pathname: url.pathname, search: url.search },
    history: { replaceState: (_s: unknown, _t: string, to: string) => replaced.push(to) },
  });
  vi.stubGlobal('sessionStorage', { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
  return replaced;
}

describe('takeRing', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('reads the ring from the address and takes it out, so a reload does not ring again', () => {
    const replaced = at('https://j.ts.net/c/conv_1?ring=run_9&say=%D0%93%D0%BE%D1%82%D0%BE%D0%B2%D0%BE.');
    expect(takeRing()).toEqual({ conversationId: 'conv_1', runId: 'run_9', say: 'Готово.' });
    expect(replaced).toEqual(['/c/conv_1']);
  });

  it('a link without the words (Discord) and a test ring with no chat', () => {
    at('https://j.ts.net/');
    expect(takeRing('https://j.ts.net/c/conv_1?ring=run_9')).toEqual({ conversationId: 'conv_1', runId: 'run_9', say: '' });
    expect(takeRing('https://j.ts.net/?ring=test')).toEqual({ conversationId: null, runId: 'test', say: '' });
    expect(takeRing('https://j.ts.net/c/conv_1')).toBeNull();
  });
});
