import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type SyntheticEvent } from 'react';
import { api } from '../api/client';
import { ChatScreen } from '../chat/ChatScreen';
import { Icon } from '../components/Icon';
import { IconButton, RelativeTime } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import type { MailItem, MailSearchResult, MailThread, MailThreadDetail, MailThreadList } from '../protocol/types';
import { DESKTOP_QUERY, useMediaQuery } from '../shell/useMediaQuery';
import { useStore } from '../store/store';
import { MailHtml } from './MailHtml';
import { participants } from './people';

type Pane = 'list' | 'thread' | 'jarvis';

/** Asked with one tap; Arsen's own words, in his language. */
const QUICK_ASKS = ['Обобщи нишката', 'Какво се иска от мен?', 'Подготви чернова на отговор'];

/** The search box speaks Outlook's syntax; this is the part of it the host understands. */
const SYNTAX: [string, string][] = [
  ['from:maria', 'sender name or address'],
  ['to:pete  cc:ops', 'recipients'],
  ['subject:budget', 'subject'],
  ['body:invoice', 'message text'],
  ['"exact phrase"', 'anywhere, as written'],
  ['budget 2027', 'every word, anywhere'],
  ['hasattachments:yes', 'with attachments'],
  ['is:unread  is:flagged', 'state'],
  ['received:today', 'yesterday, "this week", "last month"'],
  ['received:>=2026-09-01', 'or 2026-09-01..2026-09-30'],
  ['folder:Demands', 'only folders whose path has this'],
  ['from:a OR from:b', 'either'],
  ['-subject:newsletter', 'NOT'],
];

function senderOf(item: MailItem): string {
  // The first that is set AND not empty: Exchange sends "" for a name it does not know.
  return [item.from?.name, item.sender, item.from?.address].find((s) => s) ?? 'Unknown';
}

/** The message text: the body when there is one. A table preview is "" for much HTML mail. */
function textOf(item: MailItem): string {
  return [item.body, item.preview].find((s) => s?.trim()) ?? '';
}

function lastSegment(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path;
}

const PEOPLE_SHOWN = 10;

function People({ items }: { items: MailItem[] }) {
  const [all, setAll] = useState(false);
  const people = participants(items);
  if (people.length === 0) return null;
  const shown = all ? people : people.slice(0, PEOPLE_SHOWN);
  return (
    <div className="mail-people" aria-label="People on this thread">
      <span className="mail-people-label small muted">{people.length} people</span>
      {shown.map((p) => (
        <span key={p.address || p.name} className="mail-person" title={p.address || p.name}>
          {p.name && <span className="mail-person-name">{p.name}</span>}
          {p.address && <span className="mail-person-addr">{p.address}</span>}
        </span>
      ))}
      {people.length > PEOPLE_SHOWN && (
        <button type="button" className="kg-link small" onClick={() => setAll((v) => !v)}>
          {all ? 'fewer' : `+${people.length - PEOPLE_SHOWN} more`}
        </button>
      )}
    </div>
  );
}

/**
 * The mail desk: unread threads (or a search), one thread, and Jarvis beside it.
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
  const [draft, setDraft] = useState('');
  const [search, setSearch] = useState('');
  const [showSyntax, setShowSyntax] = useState(false);
  const [current, setCurrent] = useState<MailThread | null>(null);
  const [pane, setPane] = useState<Pane>('list');
  const [chatFor, setChatFor] = useState<{ key: string; conversationId: string | null; error: string | null } | null>(null);
  const [marking, setMarking] = useState<ReadonlySet<string>>(() => new Set());
  const forceRefresh = useRef(false);
  const sessionSeq = useRef(0);
  const searchRef = useRef<HTMLInputElement>(null);

  // --- the list: unread mail of every folder, or a search ---------------------------------------
  const loadUnread = useCallback(() => {
    const refresh = forceRefresh.current;
    forceRefresh.current = false;
    return api.mail.threads(account, refresh);
  }, [account]);
  const unread = useLoader<MailThreadList>(loadUnread, `mail:${account ?? ''}`);
  const loadSearch = useCallback(() => (search ? api.mail.search(search, account) : Promise.resolve(null)), [search, account]);
  const found = useLoader<MailSearchResult | null>(loadSearch, `search:${account ?? ''}:${search}`);
  const searching = search !== '';
  const listing = searching ? found : unread;
  const threads = (searching ? found.data?.threads : unread.data?.threads) ?? [];
  const accounts = unread.data?.accounts ?? found.data?.accounts ?? [];
  const mailbox = unread.data?.account ?? found.data?.account ?? account;

  const refresh = () => {
    if (searching) {
      found.reload();
      return;
    }
    forceRefresh.current = true;
    unread.reload();
  };
  const submitSearch = (e: SyntheticEvent) => {
    e.preventDefault();
    const q = draft.trim();
    setShowSyntax(false);
    if (q === search) found.reload();
    else setSearch(q);
  };
  const clearSearch = () => {
    setDraft('');
    setSearch('');
  };

  useEffect(() => {
    const focus = () => searchRef.current?.focus();
    window.addEventListener('jarvis:focus-search', focus);
    return () => window.removeEventListener('jarvis:focus-search', focus);
  }, []);

  // --- the thread ------------------------------------------------------------------------------
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
  const unreadIds = new Set(current?.entry_ids ?? []);
  for (const item of detail.data?.items ?? []) if (item.unread) unreadIds.add(item.entry_id);

  /** Mark a thread read - the open one (its unread as the thread shows them) or one from the list. */
  const markThreadRead = (thread: MailThread, ids: string[]) => {
    if (ids.length === 0) return;
    const isCurrent = current?.key === thread.key;
    const at = threads.findIndex((t) => t.key === thread.key);
    const next = threads[at + 1] ?? threads[at - 1] ?? null;
    setMarking((m) => new Set(m).add(thread.key));
    api.mail
      .markRead(ids, mailbox)
      .then((res) => {
        if (res.failed.length > 0) {
          notify(`${res.failed.length} of ${ids.length} could not be marked read: ${res.failed[0]?.error ?? ''}`, 'error');
          return;
        }
        unread.setData((d) => (d ? { ...d, threads: d.threads.filter((t) => t.key !== thread.key), unread: Math.max(0, d.unread - thread.unread_count) } : d));
        if (searching) {
          // A search keeps the thread in its results; it is just no longer unread.
          found.setData((d) => (d ? { ...d, threads: d.threads.map((t) => (t.key === thread.key ? { ...t, unread_count: 0, entry_ids: [] } : t)) } : d));
          if (isCurrent) {
            setCurrent({ ...thread, unread_count: 0, entry_ids: [] });
            detail.reload();
          }
          return;
        }
        if (!isCurrent) return;
        if (next && next.key !== thread.key) select(next);
        else {
          setCurrent(null);
          setPane('list');
        }
      })
      .catch((e: unknown) => notify(`Mark read failed: ${errorText(e)}`, 'error'))
      .finally(() =>
        setMarking((m) => {
          const s = new Set(m);
          s.delete(thread.key);
          return s;
        }),
      );
  };
  const markRead = () => {
    if (current) markThreadRead(current, [...unreadIds]);
  };

  const chatReady = chatFor !== null && chatFor.key === current?.key && chatFor.conversationId !== null && openConversationId === chatFor.conversationId;

  const count = searching ? found.data?.matches : unread.data?.unread;
  const listPane = (
    <section className="mail-list" aria-label={searching ? 'Search results' : 'Unread threads'}>
      <header className="mail-pane-head">
        <div className="mail-title">
          <h2>{searching ? 'Search' : 'Unread'}</h2>
          {count !== undefined && <span className="chip chip-outline">{count}</span>}
        </div>
        <IconButton icon="refresh" label={searching ? 'Search again' : 'Read the mailbox again'} size="sm" onClick={refresh} disabled={listing.loading} />
      </header>
      <div className="mail-tools">
        {accounts.length > 1 && (
          <select className="select" value={mailbox ?? ''} onChange={(e) => setAccount(e.target.value)} aria-label="Mailbox">
            {accounts.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        )}
        <form className="mail-search" role="search" onSubmit={submitSearch}>
          <Icon name="search" size={14} />
          <input
            ref={searchRef}
            className="input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') clearSearch();
            }}
            placeholder='Search mail: from:maria received:"this week"'
            aria-label="Search mail"
            enterKeyHint="search"
          />
          {draft || searching ? <IconButton icon="x" label="Back to unread" size="sm" onClick={clearSearch} /> : null}
          <IconButton icon="info" label="Search syntax" size="sm" active={showSyntax} onClick={() => setShowSyntax((v) => !v)} />
        </form>
        {showSyntax && (
          <div className="mail-syntax" role="note">
            <p className="small muted">Outlook&apos;s search syntax, over every folder (Sent Items too, not Deleted or Junk). Without received:, the last year. Enter searches.</p>
            <dl>
              {SYNTAX.map(([k, v]) => (
                <div key={k}>
                  <dt>
                    <button type="button" className="kg-link mono" onClick={() => setDraft((d) => `${d ? `${d} ` : ''}${k.split('  ')[0] ?? k}`)}>
                      {k}
                    </button>
                  </dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
        {searching && found.data && (
          <p className="small muted mail-scope">
            {found.data.threads.length} thread{found.data.threads.length === 1 ? '' : 's'} for <span className="mono">{found.data.query}</span>
            {found.data.days_back ? `, last ${found.data.days_back} days` : ''}
          </p>
        )}
      </div>
      {listing.error && (
        <div className="mail-error" role="alert">
          <Icon name="alert" size={14} />
          <span>{listing.error}</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={refresh}>
            Try again
          </button>
        </div>
      )}
      <div className="mail-rows" role="listbox" aria-label="Threads" tabIndex={0} onKeyDown={onListKey}>
        {listing.loading && <MailSkeleton rows={threads.length > 0 ? 1 : 6} />}
        {!listing.loading && listing.data && threads.length === 0 && (
          <div className="empty">
            <strong>{searching ? 'Nothing found' : 'Inbox zero'}</strong>
            <span>{searching ? 'Try fewer words, or open the syntax help.' : 'No unread mail in any folder.'}</span>
          </div>
        )}
        {threads.map((t) => (
          <div key={t.key} className="mail-row-wrap" role="presentation">
            <button
              id={`mail-row-${t.key}`}
              type="button"
              role="option"
              aria-selected={current?.key === t.key}
              className={`mail-row${current?.key === t.key ? ' active' : ''}${t.unread_count > 0 ? ' unread' : ''}`}
              onClick={() => select(t)}
            >
              <div className="mail-row-top">
                {t.unread_count > 0 && <span className="mail-dot" aria-label="unread" />}
                <span className="mail-from truncate">{t.senders.join(', ')}</span>
                {t.count > 1 && <span className="mail-count">{t.count}</span>}
                <span className="mail-when">{t.latest.received ? <RelativeTime ts={t.latest.received} /> : ''}</span>
              </div>
              <div className="mail-subject truncate">
                {t.flagged && <Icon name="pin" size={12} />}
                {t.has_attachments && <Icon name="paperclip" size={12} />}
                {t.subject}
              </div>
              <div className="mail-row-bottom">
                <span className="mail-preview">{t.latest.preview}</span>
                {t.folders.length > 0 && (
                  <span className="mail-folder" title={t.folders.join(', ')}>
                    <Icon name="folder" size={11} />
                    {lastSegment(t.folders[0] ?? '')}
                    {t.folders.length > 1 ? ` +${t.folders.length - 1}` : ''}
                  </span>
                )}
              </div>
            </button>
            {t.unread_count > 0 && t.entry_ids.length > 0 && (
              <IconButton
                icon="check"
                label="Mark thread read"
                size="sm"
                className="mail-row-read"
                onClick={() => markThreadRead(t, t.entry_ids)}
                disabled={marking.has(t.key)}
              />
            )}
          </div>
        ))}
        {listing.data?.capped && <p className="small muted mail-capped">{searching ? 'More hits than one search returns; narrow it down.' : 'More unread mail than one read covers; the oldest are not shown.'}</p>}
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
              <span className="small muted truncate">
                {current.senders.join(', ')}
                {current.folders.length > 0 ? ` · ${current.folders.join(', ')}` : ''}
              </span>
            </div>
            <div className="row">
              {!desktop && (
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setPane('jarvis')}>
                  <Icon name="chat" size={14} />
                  Jarvis
                </button>
              )}
              {unreadIds.size > 0 && (
                <button type="button" className="btn btn-primary btn-sm" onClick={markRead} disabled={marking.has(current.key)}>
                  <Icon name="check" size={14} />
                  {marking.has(current.key) ? 'Marking…' : 'Mark read'}
                </button>
              )}
            </div>
          </header>
          {detail.data && !detail.loading && <People key={current.key} items={detail.data.items} />}
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
            ['list', `${searching ? 'Search' : 'Unread'}${listing.data ? ` (${threads.length})` : ''}`],
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
  const [full, setFull] = useState<Record<string, MailItem | { error: string }>>({});
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, []);

  const loadFull = (item: MailItem) => {
    api.mail
      .message(item.entry_id, account)
      .then((m) => setFull((f) => ({ ...f, [item.entry_id]: m })))
      .catch((e: unknown) => setFull((f) => ({ ...f, [item.entry_id]: { error: errorText(e) } })));
  };

  const last = thread.items.length - 1;
  return (
    <>
      {thread.total > thread.items.length && <p className="small muted">{thread.total - thread.items.length} older messages are not shown.</p>}
      {thread.items.map((item, i) => {
        const isOpen = open[item.entry_id] ?? (i === last || item.unread === true);
        const got = full[item.entry_id];
        const loaded = got && !('error' in got) ? got : null;
        const html = loaded?.html ?? item.html;
        const text = loaded ? (loaded.body ?? '') : textOf(item);
        // Offer the whole message when what is shown was cut, or there is nothing to show at all.
        // The HTML body is never cut: it is the whole message already.
        const canLoad = !loaded && !html && (item.body_truncated === true || !text || (!item.body && text.length >= 3900));
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
                {html ? (
                  <MailHtml html={html} title={`Message from ${senderOf(item)}`} />
                ) : (
                  <div className="mail-text">{text ? text : <span className="muted">(no text)</span>}</div>
                )}
                {item.attachments && item.attachments.length > 0 && (
                  <div className="mail-attachments small muted">
                    <Icon name="paperclip" size={12} />
                    {item.attachments.map((a) => a.name).join(', ')}
                  </div>
                )}
                {item.body_error && <div className="field-error">{item.body_error}</div>}
                {got && 'error' in got && <div className="field-error">{got.error}</div>}
                {canLoad && (
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
