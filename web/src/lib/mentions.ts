/**
 * The "@" being typed in the composer, and which sessions it could mean.
 *
 * Pure, and its own file: the component next to it re-renders on every keystroke and these are
 * what the tests hold on to. A session is a chat of Jarvis's, addressed by the handle the core
 * derives from its title (features/sessions.py).
 */
import type { SessionRef } from '../protocol/types';

export interface MentionQuery {
  /** Where the "@" is in the text, and what has been typed after it. */
  at: number;
  word: string;
}

/** The "@word" being typed at the caret, if there is one. */
export function mentionAt(text: string, caret: number): MentionQuery | null {
  const upto = text.slice(0, caret);
  const at = upto.lastIndexOf('@');
  if (at < 0) return null;
  const before = at === 0 ? '' : upto[at - 1];
  if (before && !/[\s(]/.test(before)) return null; // an e-mail address is not a mention
  const word = upto.slice(at + 1);
  return /[\s]/.test(word) ? null : { at, word };
}

export function matches(sessions: readonly SessionRef[], word: string, exclude: string | null): SessionRef[] {
  const w = word.toLowerCase();
  return sessions
    .filter((s) => s.conversation_id !== exclude)
    .filter((s) => !w || s.handle.includes(w) || s.title.toLowerCase().includes(w))
    .slice(0, 6);
}

