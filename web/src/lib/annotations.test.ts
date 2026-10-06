import { describe, expect, it } from 'vitest';
import {
  DEFAULT_REVISE,
  ONE_PASS,
  commentsAttachmentName,
  commentsDocument,
  composeWithAnnotations,
  revisionAsk,
  type Annotation,
} from './annotations';

const note = (over: Partial<Annotation>): Annotation => ({
  id: 'a',
  messageId: 'm1',
  quote: 'Dear John',
  comment: 'make it Dear Mr. Smith',
  start: 0,
  end: 9,
  ...over,
});

describe('commentsDocument', () => {
  it('quotes the passage and adds the comment', () => {
    expect(commentsDocument([note({})])).toBe(`A comment on your reply:\n\n1. On this part:\n> Dear John\n\n   My comment: make it Dear Mr. Smith\n\n${ONE_PASS}`);
  });

  it('asks for one pass over all comments, whatever Arsen types with them', () => {
    expect(commentsDocument([note({}), note({ id: 'b', start: 20 })])).toContain('ONE pass');
    expect(composeWithAnnotations([note({})], 'my own words')).toContain(ONE_PASS);
  });

  it('orders comments down the page and quotes every line of a multi-line passage', () => {
    const out = commentsDocument([note({ id: 'b', quote: 'Best,\n\nArsen', comment: 'Kind regards', start: 50, end: 62 }), note({ id: 'a', start: 0 })]);
    expect(out.startsWith('2 comments on your reply:')).toBe(true);
    expect(out.indexOf('Dear John')).toBeLessThan(out.indexOf('Best,'));
    expect(out).toContain('> Best,\n>\n> Arsen');
  });

  it('says something even when the comment is empty', () => {
    expect(commentsDocument([note({ comment: '  ' })])).toContain('My comment: (change this)');
  });
});

describe('the message that goes with the attachment', () => {
  it('is what Arsen typed, or one ask for one complete revision', () => {
    expect(revisionAsk('  Also shorter. ')).toBe('Also shorter.');
    expect(revisionAsk('')).toBe(DEFAULT_REVISE);
    expect(DEFAULT_REVISE).toContain('in one reply');
  });

  it('names the attachment by how many comments it holds', () => {
    expect(commentsAttachmentName(1)).toBe('Comment on the reply');
    expect(commentsAttachmentName(3)).toBe('3 comments on the reply');
  });
});

describe('composeWithAnnotations (no attachment possible)', () => {
  it('leaves the text alone when there are no comments', () => {
    expect(composeWithAnnotations([], '  hello ')).toBe('hello');
  });

  it('puts the comments in the text and points the ask at them, not at a file', () => {
    const out = composeWithAnnotations([note({})], '');
    expect(out.startsWith(commentsDocument([note({})]))).toBe(true);
    expect(out).toContain('my comments above');
    expect(out).not.toContain('attached');
  });

  it('ends with what Arsen typed', () => {
    expect(composeWithAnnotations([note({})], 'Also shorter overall.').endsWith('\n\nAlso shorter overall.')).toBe(true);
  });
});
