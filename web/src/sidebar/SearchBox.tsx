import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { Highlight } from '../components/Highlight';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import type { Conversation, SearchHit } from '../protocol/types';
import { useStore } from '../store/store';
import { ConversationRow } from './ConversationRow';

interface Props {
  query: string;
  onQuery: (q: string) => void;
}

/** One search box: filters titles as you type, searches message text on the server after a pause. */
export function SearchBox({ query, onQuery }: Props) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const focus = () => {
      ref.current?.focus();
      ref.current?.select();
    };
    window.addEventListener('jarvis:focus-search', focus);
    return () => window.removeEventListener('jarvis:focus-search', focus);
  }, []);
  return (
    <div className="search-box">
      <Icon name="search" size={14} />
      <input
        ref={ref}
        className="search-input"
        type="search"
        value={query}
        onChange={(e) => onQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onQuery('');
        }}
        placeholder="Search chats and messages"
        aria-label="Search conversations and messages"
        autoComplete="off"
      />
      {query && <IconButton icon="x" label="Clear search" size="sm" onClick={() => onQuery('')} />}
    </div>
  );
}

interface Remote {
  q: string;
  hits: SearchHit[] | null;
  error: string | null;
}

/**
 * Results for the query: chats whose title matches, then the messages inside chats.
 *
 * Titles come from two places on purpose. Locally, over the conversations already in the store,
 * so the list narrows on the keystroke; and from the server, which searches every title in the
 * database — including the archived ones this client has not loaded, which the local pass alone
 * could never find. Message hits are the server's only, and clicking one lands on the message
 * that matched rather than at the bottom of a long transcript.
 */
export function SearchResults({ query }: { query: string }) {
  const openAt = useStore((s) => s.openConversationAt);
  const conversations = useStore((s) => s.conversations);
  const openId = useStore((s) => s.openConversationId);
  // Results are tagged with the query they answer, so a stale answer is never shown for a new query.
  const [remote, setRemote] = useState<Remote>({ q: '', hits: null, error: null });
  const q = query.trim();

  useEffect(() => {
    if (q.length < 2) return;
    let cancelled = false;
    const t = setTimeout(() => {
      api.conversations
        .search(q)
        .then((res) => {
          if (!cancelled) setRemote({ q, hits: res, error: null });
        })
        .catch((e: unknown) => {
          if (!cancelled) setRemote({ q, hits: null, error: e instanceof Error ? e.message : String(e) });
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q]);

  const current = remote.q === q ? remote : null;
  const server = q.length >= 2 ? (current?.hits ?? null) : null;
  const lower = q.toLowerCase();

  // Local titles first (instant), then the server's (complete), de-duplicated by id.
  const chats: Conversation[] = [];
  const seen = new Set<string>();
  for (const c of [
    // The core never returns an incognito chat; the local list must not either.
    ...Object.values(conversations).filter((x) => !x.incognito && x.title.toLowerCase().includes(lower)),
    ...(server ?? []).filter((h) => h.matched === 'title').map((h) => h.conversation),
  ]) {
    if (seen.has(c.id)) continue;
    seen.add(c.id);
    chats.push(c);
  }
  chats.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const messageHits = (server ?? []).filter((h) => h.matched === 'message' && !seen.has(h.conversation.id));
  const pending = q.length >= 2 && current === null;

  return (
    <div className="search-results" aria-live="polite">
      {chats.length > 0 && (
        <div className="folder-group-label">
          Chats · {chats.length}
        </div>
      )}
      {chats.map((c) => (
        <ConversationRow key={c.id} conversation={c} active={c.id === openId} />
      ))}
      {messageHits.length > 0 && (
        <div className="folder-group-label">
          In messages · {messageHits.length}
        </div>
      )}
      {messageHits.map((h) => (
        <button
          key={`${h.conversation.id}:${h.message_id}`}
          type="button"
          className="search-hit"
          onClick={() => void openAt(h.conversation.id, h.message_id)}
        >
          <span className="truncate search-hit-title">{h.conversation.title}</span>
          {h.snippet && (
            <span className="search-snippet">
              <Highlight text={h.snippet} query={q} />
            </span>
          )}
        </button>
      ))}
      {pending && chats.length === 0 && <div className="empty small">Searching…</div>}
      {current?.error && <div className="field-error">{current.error}</div>}
      {!pending && q.length >= 2 && chats.length === 0 && messageHits.length === 0 && (
        <div className="empty small">Nothing matches “{q}”.</div>
      )}
      {q.length > 0 && q.length < 2 && <div className="empty small">Type two or more characters.</div>}
    </div>
  );
}
