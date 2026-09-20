import { describe, expect, it } from 'vitest';
import { matches, mentionAt } from './mentions';
import type { SessionRef } from '../protocol/types';

const session = (handle: string, title: string, over: Partial<SessionRef> = {}): SessionRef => ({
  conversation_id: `c_${handle}`,
  handle,
  title,
  activity: 'idle',
  archived: false,
  message_count: 3,
  updated_at: '2026-09-19T10:00:00Z',
  ...over,
});

const SESSIONS = [session('домо', 'Домо'), session('домо-ремонт', 'Домо ремонт'), session('patzer', 'Patzer chess')];

describe('mentionAt', () => {
  it('finds the handle being typed at the caret', () => {
    expect(mentionAt('@дом', 4)).toEqual({ at: 0, word: 'дом' });
    expect(mentionAt('кажи на @дом', 12)).toEqual({ at: 8, word: 'дом' });
    expect(mentionAt('@', 1)).toEqual({ at: 0, word: '' });
  });
  it('is not fooled by an e-mail address or a finished mention', () => {
    expect(mentionAt('arsen@gmail.com', 15)).toBeNull();
    expect(mentionAt('@домо кажи им', 13)).toBeNull(); // the space ended it
  });
  it('reads the word at the caret, not at the end of the line', () => {
    expect(mentionAt('@домо и @pat нещо', 12)).toEqual({ at: 8, word: 'pat' });
  });
});

describe('matches', () => {
  it('offers the sessions whose handle or title contains the word', () => {
    expect(matches(SESSIONS, 'дом', null).map((s) => s.handle)).toEqual(['домо', 'домо-ремонт']);
    expect(matches(SESSIONS, 'chess', null).map((s) => s.handle)).toEqual(['patzer']);
  });
  it('an empty word offers everything, and the open chat is never itself a candidate', () => {
    expect(matches(SESSIONS, '', null)).toHaveLength(3);
    expect(matches(SESSIONS, '', 'c_домо').map((s) => s.handle)).toEqual(['домо-ремонт', 'patzer']);
  });
});
