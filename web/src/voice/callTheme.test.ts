import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

/**
 * The locked call screen is black whatever the theme (features.css, .call-locked), so every
 * colour a part of it draws with must be the dark theme's there too. On a phone on the light
 * theme the slides came out white and the "go on" amber brown on the black (2026-11-14, alpha).
 */
const css = (name: string) => readFileSync(new URL(`../styles/${name}`, import.meta.url), 'utf8');

function block(source: string, selector: string): string {
  const at = source.indexOf(`${selector} {`);
  if (at < 0) throw new Error(`${selector} not found`);
  return source.slice(at, source.indexOf('}', at));
}

describe('the locked call screen', () => {
  it('redefines every themed token the call screen uses, so a light theme does not leak onto the black', () => {
    // What the light theme changes: those are the tokens that differ between themes.
    const themed = new Set([...block(css('tokens.css'), ":root[data-theme='light']").matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
    const locked = new Set([...block(css('features.css'), '.call-locked').matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
    const features = css('features.css');
    const used = new Set<string>();
    // Every rule of the call screen (not the incoming-call card, which is not on the black).
    for (const rule of features.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = rule[1] ?? '';
      if (!/(^|[\s,])\.call[-\s{.:,]/.test(selector) || selector.includes('call-incoming')) continue;
      for (const v of (rule[2] ?? '').matchAll(/var\((--[\w-]+)/g)) if (themed.has(v[1] ?? '')) used.add(v[1] ?? '');
    }
    expect(used.size).toBeGreaterThan(5);
    expect([...used].filter((t) => !locked.has(t)).sort()).toEqual([]);
  });
});
