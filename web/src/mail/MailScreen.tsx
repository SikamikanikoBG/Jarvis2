import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { api } from '../api/client';
import { ChatScreen } from '../chat/ChatScreen';
import { Icon } from '../components/Icon';
import { IconButton, RelativeTime } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import type { MailItem, MailThread, MailThreadDetail, MailThreadList } from '../protocol/types';
import { DESKTOP_QUERY, useMediaQuery } from '../shell/useMediaQuery';
import { useStore } from '../store/store';

type Pane = 'list' | 'thread' | 'jarvis';

/** Asked with one tap; Arsen's own words, in his language. */
const QUICK_ASKS = ['Обобщи нишката', 'Какво се иска от мен?', 'Подготви чернова на отговор'];

function senderOf(item: MailItem): string {
  // The first that is set AND not empty: Exchange sends "" for a name it does not know.
  return [item.from?.name, item.sender, item.from?.address].find((s) => s) ?? 'Unknown';
}

function matches(t: MailThread, q: string): boolean {
  if (!q) return true;
  const hay = `${t.subject} ${t.senders.join(' ')} ${t.latest.preview}`.toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((w) => hay.includes(w));
}

/**
 * The mail desk: unread threads, one thread, and Jarvis beside it.
 *
 * The chat on the right is the real ChatScreen - the one every conversation uses - bound to a
 * conversation the core keeps for this thread (POST /api/mail/session). So streaming, tool cards,
 * attachments, confirmations and the composer are simply there, and the thread reaches the model
 * through the conversation's instructions rather than being pasted into a message. V1 learned
 * the other way round what a second chat client costs (journal_email_dock.md).
 */
export function MailScreen() {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const notify = useStore((s) => s.notify);
  const openConversation = useStore((s) => s.openConversation);
  const openConversationId = useStore((s) => s.openConversationId);
  const send = useStore((s) => s.send);
  const setView = useStore((s) => s.setView);

  const [account, setAccount] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [current, setCurrent] = useState<MailThread | null>(null);
  const [pane, setPane] = useState<Pane>('list');
  const [chatFor, setChatFor] = useState<{ key: string; conversationId: string | null; error: string | null } | null>(null);
  const [marking, setMarking] = useState(false);
  const forceRefresh = useRef(false);
  const sessionSeq = useRef(0);
  const searchRef = useRef<HTMLInputElement>(null);

  // --- the list --------------------------------------------------------------------------------
  const loadList = useCallback(() => {
    const refresh = forceRefresh.current;
    forceRefresh.current = false;
    return api.mail.threads(account, refresh);
  }, [account]);
  const list = useLoader<MailThreadList>(loadList, `mail:${account ?? ''}`);
  const threads = useMemo(() => (list.data?.threads ?? []).filter((t) => matches(t, query)), [list.data, query]);
  const refresh = () => {
    forceRefresh.current = true;
    list.reload();
  };

  useEffect(() => {
    const focus = () => searchRef.current?.focus();
    window.addEventListener('jarvis:focus-search', focus);
    return () => window.removeEventListener('jarvis:focus-search', focus);
  }, []);

  // --- the thread ------------------------------------------------------------------------------
  const mailbox = list.data?.account ?? account;
  const loadThread = useCallback(() => (current ? api.mail.thread(current.latest.entry_id, mailbox) : Promise.resolve(null)), [current, mailbox]);
  const detail = useLoader<MailThreadDetail | null>(loadThread, `thread:${current?.key ?? ''}`);

  // --- selecting ---------------------------------------------------------------------------------
  const select = (t: MailThread) => {
    setCurrent(t);
    if (!desktop) setPane('thread');
    // The chat beside it: found or created by the core, then opened in place.
    const seq = ++sessionSeq.current;
    setChatFor({ key: t.key, conversationId: null, error: null });
    api.mail
      .session(t.latest.entry_id, mailbox)
      .then((conv) => {
        if (seq !== sessionSeq.current) return;
        setChatFor({ key: t.key, conversationId: conv.id, error: null });
        void openConversation(conv.id, { stay: true });
      })
      .catch((e: unknown) => {
        if (seq === sessionSeq.current) setChatFor({ key: t.key, conversationId: null, error: errorText(e) });
      });
  };

  const onListKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const at = current ? threads.findIndex((t) => t.key === current.key) : -1;
    const next = threads[e.key === 'ArrowDown' ? Math.min(threads.length - 1, at + 1) : Math.max(0, at - 1)];
    if (next && next.key !== current?.key) {
      select(next);
      document.getElementById(`mail-row-${next.key}`)?.scrollIntoView({ block: 'nearest' });
    }
  };

  // --- read ---------------------------------------------------------------------------------------
  const markRead = () => {
    if (!current) return;
    const ids = new Set(current.entry_ids);
    for (const item of detail.data?.items ?? []) if (item.unread) ids.add(item.entry_id);
    const at = threads.findIndex((t) => t.key === current.key);
    const next = threads[at + 1] ?? threads[at - 1] ?? null;
    setMarking(true);
    api.mail
      .markRead([...ids], mailbox)
      .then((res) => {
        if (res.failed.length > 0) {
          notify(`${res.failed.length} of ${ids.size} could not be marked read: ${res.failed[0]?.error ?? ''}`, 'error');
          return;
        }
        list.setData((d) => (d ? { ...d, threads: d.threads.filter((t) => t.key !== current.key), unread: Math.max(0, d.unread - current.unread_count) } : d));
        if (next && next.key !== current.key) select(next);
        else {
          setCurrent(null);
          setPane('list');
        }
      })
      .catch((e: unknown) => notify(`Mark read failed: ${errorText(e)}`, 'error'))
      .finally(() => setMarking(false));
  };

  const chatReady = chatFor !== null && chatFor.key === current?.key && chatFor.conversationId !== null && openConversationId === chatFor.conversationId;

  const listPane = (
    <section className="mail-list" aria-label="Unread threads">
      <header className="mail-pane-head">
        <div className="mail-title">
          <h2>Unread</h2>
          {list.data && <span className="chip chip-outline">{list.data.unread}</span>}
        </div>
        <IconButton icon="refresh" label="Read the mailbox again" size="sm" onClick={refresh} disabled={list.loading} />
      </header>
      <div className="mail-tools">
        {list.data && list.data.accounts.length > 1 && (
          <select className="select" value={list.data.account} onChange={(e) => setAccount(e.target.value)} aria-label="Mailbox">
            {list.data.accounts.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        )}
        <div className="mail-search">
          <Icon name="search" size={14} />
          <input ref={searchRef} className="input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter by subject, sender, text" aria-label="Filter threads" />
        </div>
      </div>
      {list.error && (
        <div className="mail-error" role="alert">
          <Icon name="alert" size={14} />
          <span>{list.error}</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={refresh}>
            Try again
          </button>
        </div>
      )}
      <div className="mail-rows" role="listbox" aria-label="Threads" tabIndex={0} onKeyDown={onListKey}>
        {list.loading && !list.data && <MailSkeleton />}
        {list.data && threads.length === 0 && (
          <div className="empty">
            <strong>{query ? 'Nothing matches' : 'Inbox zero'}</strong>
            <span>{query ? 'Clear the filter to see every unread thread.' : 'No unread mail in the inbox.'}</span>
          </div>
        )}
        {threads.map((t) => (
          <button
            key={t.key}
            id={`mail-row-${t.key}`}
            type="button"
            role="option"
            aria-selected={current?.key === t.key}
            className={`mail-row${current?.key === t.key ? ' active' : ''}`}
            onClick={() => select(t)}
          >
            <div className="mail-row-top">
              <span className="mail-from truncate">{t.senders.join(', ')}</span>
              {t.unread_count > 1 && <span className="mail-count">{t.unread_count}</span>}
              <span className="mail-when">{t.latest.received ? <RelativeTime ts={t.latest.received} /> : ''}</span>
            </div>
            <div className="mail-subject truncate">
              {t.flagged && <Icon name="pin" size={12} />}
              {t.has_attachments && <Icon name="paperclip" size={12} />}
              {t.subject}
            </div>
            <div className="mail-preview">{t.latest.preview}</div>
          </button>
        ))}
        {list.data?.capped && <p className="small muted mail-capped">More unread mail than one read covers; the oldest are not shown.</p>}
      </div>
    </section>
  );

  const threadPane = (
    <section className="mail-thread" aria-label="Thread">
      {!current ? (
        <div className="empty mail-placeholder">
          <Icon name="mail" size={28} />
          <strong>Pick a thread</strong>
          <span>It opens here, and Jarvis beside it already knows it.</span>
        </div>
      ) : (
        <>
          <header className="mail-pane-head">
            {!desktop && <IconButton icon="chevronLeft" label="Back to the list" size="sm" onClick={() => setPane('list')} />}
            <div className="mail-thread-title">
              <h2>{detail.data?.subject ?? current.subject}</h2>
              <span className="small muted truncate">{current.senders.join(', ')}</span>
            </div>
            <div className="row">
              {!desktop && (
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setPane('jarvis')}>
                  <Icon name="chat" size={14} />
                  Jarvis
                </button>
              )}
              <button type="button" className="btn btn-primary btn-sm" onClick={markRead} disabled={marking}>
                <Icon name="check" size={14} />
                {marking ? 'Marking…' : 'Mark read'}
              </button>
            </div>
          </header>
          <div className="mail-messages">
            {detail.error && (
              <div className="mail-error" role="alert">
                <Icon name="alert" size={14} />
                <span>{detail.error}</span>
                <button type="button" className="btn btn-secondary btn-sm" onClick={detail.reload}>
                  Try again
                </button>
              </div>
            )}
            {detail.loading && !detail.error && <MailSkeleton rows={2} />}
            {detail.data && !detail.loading && <ThreadMessages key={current.key} thread={detail.data} account={mailbox} />}
          </div>
        </>
      )}
    </section>
  );

  const jarvisPane = (
    <section className="mail-jarvis" aria-label="Jarvis on this thread">
      {!current ? (
        <div className="empty mail-placeholder">
          <Icon name="chat" size={28} />
          <strong>Jarvis works on the open thread</strong>
          <span>Summaries, a draft reply, what is being asked - each thread keeps its own chat.</span>
        </div>
      ) : chatFor?.error ? (
        <div className="mail-error" role="alert">
          <Icon name="alert" size={14} />
          <span>{chatFor.error}</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => select(current)}>
            Try again
          </button>
        </div>
      ) : !chatReady ? (
        <div className="empty mail-placeholder">
          <span>Opening the chat for this thread…</span>
        </div>
      ) : (
        <>
          <header className="mail-pane-head">
            {!desktop && <IconButton icon="chevronLeft" label="Back to the thread" size="sm" onClick={() => setPane('thread')} />}
            <div className="mail-asks">
              {QUICK_ASKS.map((q) => (
                <button key={q} type="button" className="chip-toggle" onClick={() => send(q)}>
                  {q}
                </button>
              ))}
            </div>
            <IconButton icon="chatDots" label="Open in Chat" size="sm" onClick={() => setView('chat')} />
          </header>
          <ChatScreen />
        </>
      )}
    </section>
  );

  if (desktop) {
    return (
      <div className="mail-desk">
        {listPane}
        {threadPane}
        {jarvisPane}
      </div>
    );
  }
  return (
    <div className="mail-desk mobile">
      <div className="seg mail-tabs" role="tablist" aria-label="Mail panes">
        {(
          [
            ['list', `Inbox${list.data ? ` (${list.data.threads.length})` : ''}`],
            ['thread', 'Thread'],
            ['jarvis', 'Jarvis'],
          ] as [Pane, string][]
        ).map(([p, label]) => (
          <button key={p} type="button" role="tab" aria-selected={pane === p} className={pane === p ? 'active' : ''} onClick={() => setPane(p)} disabled={p !== 'list' && !current}>
            {label}
          </button>
        ))}
      </div>
      {pane === 'list' ? listPane : pane === 'thread' ? threadPane : jarvisPane}
    </div>
  );
}

/** The messages of a thread, oldest first: the newest and the unread open, the rest one line each. */
function ThreadMessages({ thread, account }: { thread: MailThreadDetail; account: string | null }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [full, setFull] = useState<Record<string, string | { error: string }>>({});
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, []);

  const loadFull = (item: MailItem) => {
    api.mail
      .message(item.entry_id, account)
      .then((m) => setFull((f) => ({ ...f, [item.entry_id]: m.body ?? '' })))
      .catch((e: unknown) => setFull((f) => ({ ...f, [item.entry_id]: { error: errorText(e) } })));
  };

  const last = thread.items.length - 1;
  return (
    <>
      {thread.total > thread.items.length && <p className="small muted">{thread.total - thread.items.length} older messages are not shown.</p>}
      {thread.items.map((item, i) => {
        const isOpen = open[item.entry_id] ?? (i === last || item.unread === true);
        const body = full[item.entry_id];
        const text = typeof body === 'string' ? body : (item.preview ?? item.body ?? '');
        const maybeCut = typeof body !== 'string' && text.length >= 3900;
        return (
          <article key={item.entry_id} className={`mail-msg${item.mine ? ' mine' : ''}${item.unread ? ' unread' : ''}${isOpen ? ' open' : ''}`}>
            <button type="button" className="mail-msg-head" onClick={() => setOpen((o) => ({ ...o, [item.entry_id]: !isOpen }))} aria-expanded={isOpen}>
              {item.unread && <span className="mail-dot" aria-label="unread" />}
              <span className="mail-from truncate">{item.mine ? `${senderOf(item)} (you)` : senderOf(item)}</span>
              {item.has_attachments && <Icon name="paperclip" size={12} />}
              <span className="mail-when">{item.received ? <RelativeTime ts={item.received} /> : ''}</span>
            </button>
            {isOpen ? (
              <div className="mail-msg-body">
                {Boolean(item.to) || Boolean(item.cc) ? (
                  <div className="mail-recipients small muted">
                    {item.to && <div className="truncate">To: {item.to}</div>}
                    {item.cc && <div className="truncate">Cc: {item.cc}</div>}
                  </div>
                ) : null}
                <div className="mail-text">{text ? text : <span className="muted">(no text)</span>}</div>
                {typeof body === 'object' && <div className="field-error">{body.error}</div>}
                {(maybeCut || item.has_attachments) && typeof body !== 'string' && (
                  <button type="button" className="kg-link" onClick={() => loadFull(item)}>
                    Show the whole message
                  </button>
                )}
              </div>
            ) : (
              <div className="mail-msg-snippet truncate">{text}</div>
            )}
          </article>
        );
      })}
      <div ref={bottom} />
    </>
  );
}

function MailSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="mail-skeleton" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="mail-skeleton-row" />
      ))}
    </div>
  );
}
