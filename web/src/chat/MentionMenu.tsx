import { useEffect, useMemo, useState } from 'react';
import { Icon } from '../components/Icon';
import { matches, type MentionQuery } from '../lib/mentions';
import type { SessionRef } from '../protocol/types';

/**
 * The @ menu over the composer: which of Jarvis's chats an "@" is about.
 *
 * Every chat is a session with a handle made from its title (the core says what it is —
 * `GET /api/sessions` — so the two ends never slug a title differently). Typing "@дом" offers
 * the chats that match; Enter or Tab completes it, and Jarvis reads the finished "@домо" as the
 * session to talk to. It completes what Arsen types; it does not send anything by itself.
 */
export function MentionMenu({
  sessions,
  query,
  exclude,
  onPick,
  onClose,
}: {
  sessions: readonly SessionRef[];
  query: MentionQuery;
  exclude: string | null;
  onPick: (s: SessionRef) => void;
  onClose: () => void;
}) {
  const rows = useMemo(() => matches(sessions, query.word, exclude), [sessions, query.word, exclude]);
  // The highlight starts at the top for each word typed: the composer mounts this with the word
  // as its key, so the state resets by itself rather than in an effect one frame late.
  const [i, setI] = useState(0);
  // The composer owns the keyboard; this listens in the capture phase so Enter picks a session
  // instead of sending the half-typed message.
  useEffect(() => {
    if (rows.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        setI((n) => (n + (e.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length);
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        const pick = rows[i];
        if (pick) {
          e.preventDefault();
          e.stopPropagation();
          onPick(pick);
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [rows, i, onPick, onClose]);

  if (rows.length === 0) return null;
  return (
    <div className="mention-menu" role="listbox" aria-label="Sessions">
      {rows.map((s, n) => (
        <button
          key={s.conversation_id}
          type="button"
          role="option"
          aria-selected={n === i}
          className={`mention-row${n === i ? ' active' : ''}`}
          onMouseEnter={() => setI(n)}
          onMouseDown={(e) => {
            e.preventDefault(); // keep the caret in the textarea
            onPick(s);
          }}
        >
          <Icon name={s.activity === 'running' ? 'activity' : 'chat'} size={14} />
          <span className="mention-handle">@{s.handle}</span>
          <span className="mention-title">{s.title}</span>
          {s.activity !== 'idle' && <span className="mention-state">{s.activity === 'running' ? 'working' : 'waiting'}</span>}
        </button>
      ))}
    </div>
  );
}
