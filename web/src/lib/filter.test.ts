import { describe, expect, it } from 'vitest';
import { matchesQuery } from './filter';

describe('matchesQuery', () => {
  it('keeps everything when the query is empty or only spaces', () => {
    expect(matchesQuery('', 'anything')).toBe(true);
    expect(matchesQuery('   ', 'anything')).toBe(true);
    // Even a row with nothing in any of its fields.
    expect(matchesQuery('', null, undefined)).toBe(true);
  });

  it('ignores case and matches inside words', () => {
    expect(matchesQuery('BRIEF', 'Morning brief')).toBe(true);
    expect(matchesQuery('orn', 'Morning brief')).toBe(true);
    expect(matchesQuery('evening', 'Morning brief')).toBe(false);
  });

  it('needs every word, in any order and from any field', () => {
    expect(matchesQuery('brief morning', 'Morning brief')).toBe(true);
    expect(matchesQuery('morning revolut', 'Morning brief', 'Check the Revolut feed')).toBe(true);
    expect(matchesQuery('morning missing', 'Morning brief', 'Check the Revolut feed')).toBe(false);
  });

  it('never lets a term span the join between two fields', () => {
    // "briefcheck" would match a naive concatenation, and it is not in either field.
    expect(matchesQuery('briefcheck', 'brief', 'check')).toBe(false);
  });

  it('skips absent fields instead of matching "null" or "undefined"', () => {
    expect(matchesQuery('null', 'Morning brief', null)).toBe(false);
    expect(matchesQuery('undefined', 'Morning brief', undefined)).toBe(false);
    expect(matchesQuery('brief', null, 'Morning brief', undefined)).toBe(true);
  });
});
