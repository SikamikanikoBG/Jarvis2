import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import type { SearchHit } from '../protocol/types';
import { useStore } from '../store/store';

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
        placeholder="Search chats"
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

/** Server-side message hits for the query (debounced); title hits come from the local list. */
export function SearchResults({ query }: { query: string }) {
  const open = useStore((s) => s.openConversation);
  const conversations = useStore((s) => s.conversations);
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
          if (!cancelled) setRemote({ q, hits: res.filter((h) => h.matched === 'message'), error: null });
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

  const lower = q.toLowerCase();
  const titleHits = Object.values(conversations)
    .filter((c) => c.title.toLowerCase().includes(lower))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const current = remote.q === q ? remote : null;
  const hits = q.length >= 2 ? current?.hits : null;

  return (
    <div className="search-results" aria-live="polite">
      {titleHits.length > 0 && <div className="folder-group-label">Chats</div>}
      {titleHits.map((c) => (
        <button key={c.id} type="button" className="search-hit" onClick={() => void open(c.id)}>
          <span className="truncate">{c.title}</span>
        </button>
      ))}
      {hits?.length ? <div className="folder-group-label">In messages</div> : null}
      {hits?.map((h) => (
        <button key={`${h.conversation.id}:${h.message_id}`} type="button" className="search-hit" onClick={() => void open(h.conversation.id)}>
          <span className="truncate">{h.conversation.title}</span>
          {h.snippet && <span className="search-snippet">{h.snippet}</span>}
        </button>
      ))}
      {current?.error && <div className="field-error">{current.error}</div>}
      {q.length >= 2 && titleHits.length === 0 && hits?.length === 0 && <div className="empty small">Nothing matches “{q}”.</div>}
      {q.length > 0 && q.length < 2 && <div className="empty small">Type two or more characters.</div>}
    </div>
  );
}
