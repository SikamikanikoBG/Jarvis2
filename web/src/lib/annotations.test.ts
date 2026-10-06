import { describe, expect, it } from 'vitest';
import { DEFAULT_REVISE, composeWithAnnotations, locateOffsets, type Annotation } from './annotations';

const note = (over: Partial<Annotation>): Annotation => ({
  id: 'a',
  messageId: 'm1',
  quote: 'Dear John',
  comment: 'make it Dear Mr. Smith',
  start: 0,
  end: 9,
  ...over,
});

describe('composeWithAnnotations', () => {
  it('leaves the text alone when there are no comments', () => {
    expect(composeWithAnnotations([], '  hello ')).toBe('hello');
  });

  it('quotes the passage, adds the comment and a default ask', () => {
    const out = composeWithAnnotations([note({})], '');
    expect(out).toBe(`A comment on your reply:\n\n1. On this part:\n> Dear John\n\n   My comment: make it Dear Mr. Smith\n\n${DEFAULT_REVISE}`);
  });

  it('uses what Arsen typed instead of the default ask', () => {
    const out = composeWithAnnotations([note({})], 'Also shorter overall.');
    expect(out.endsWith('Also shorter overall.')).toBe(true);
    expect(out).not.toContain(DEFAULT_REVISE);
  });

  it('orders comments down the page and quotes every line of a multi-line passage', () => {
    const out = composeWithAnnotations(
      [note({ id: 'b', quote: 'Best,\n\nArsen', comment: 'Kind regards', start: 50, end: 62 }), note({ id: 'a', start: 0 })],
      '',
    );
    expect(out.startsWith('2 comments on your reply:')).toBe(true);
    expect(out.indexOf('Dear John')).toBeLessThan(out.indexOf('Best,'));
    expect(out).toContain('> Best,\n>\n> Arsen');
  });

  it('says something even when the comment is empty', () => {
    expect(composeWithAnnotations([note({ comment: '  ' })], '')).toContain('My comment: (change this)');
  });
});

describe('locateOffsets', () => {
  // "Hello " | "big" | " world" | "Second line" — the text nodes of two rendered paragraphs.
  const lengths = [6, 3, 6, 11];

  it('finds a span that crosses text nodes', () => {
    expect(locateOffsets(lengths, 7, 21)).toEqual({ startNode: 1, startOffset: 1, endNode: 3, endOffset: 6 });
  });

  it('puts a start on a node boundary in the next node', () => {
    expect(locateOffsets(lengths, 6, 9)).toEqual({ startNode: 1, startOffset: 0, endNode: 1, endOffset: 3 });
  });

  it('ends exactly at the end of the text', () => {
    expect(locateOffsets(lengths, 15, 26)).toEqual({ startNode: 3, startOffset: 0, endNode: 3, endOffset: 11 });
  });

  it('refuses spans past the end, empty or reversed', () => {
    expect(locateOffsets(lengths, 5, 500)).toBeNull();
    expect(locateOffsets(lengths, 5, 5)).toBeNull();
    expect(locateOffsets(lengths, 9, 2)).toBeNull();
    expect(locateOffsets([], 0, 1)).toBeNull();
  });
});
