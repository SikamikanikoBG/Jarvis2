import { create } from 'zustand';
import { ApiError, api, describeError } from '../api/client';
import { WsClient, type ConnectionState } from '../api/ws';
import { conversationToMarkdown, downloadText, safeFilename } from '../lib/export';
import { notifyDesktop, readNotifyPref, writeNotifyPref } from '../lib/notify';
import { navigate, parseLocation, rememberConversation, type View } from '../lib/router';
import { applyThemePref, isPanelMode, readThemePref, type ThemePref } from '../lib/theme';
import type { ThinkChoice } from '../lib/think';
import type { Attachment, Conversation, ConversationSummary, Message, Run, RunScopedEvent, ServerEvent } from '../protocol/types';
import { isRunScoped, isTerminal } from '../protocol/types';
import { applyFeatureEvents, initialFeatureState, type FeatureState } from './features';
import { applyServerEvents } from './reducer';
import { selectActiveRun, selectSendRefusal } from './selectors';
import { NEW_CONVERSATION_KEY, initialChatState, omit, type ChatState, type LocalMessage } from './state';

export interface Notice {
  id: number;
  level: 'info' | 'error';
  text: string;
}

export interface UiState {
  view: View;
  connection: ConnectionState;
  connectionAttempt: number;
  sidebarOpen: boolean;
  inspectorRunId: string | null;
  conversationsLoaded: boolean;
  archivedLoaded: boolean;
  loadingMessagesFor: string | null;
  /** Runs whose persisted events were fetched from REST. */
  runEventsLoaded: Record<string, boolean | undefined>;
  notice: Notice | null;
  themePref: ThemePref;
  panelMode: boolean;
  version: string | null;
  /** Per-conversation thinking override for new messages (absent = role default). */
  thinkChoice: Record<string, ThinkChoice | undefined>;
  /** Compaction summary per conversation; `undefined` = not fetched, `null` = none. */
  summaries: Record<string, ConversationSummary | null | undefined>;
  /** Mobile "More" navigation sheet. */
  moreOpen: boolean;
  /** "Edit and resend": the message being edited; sending forks the conversation before it. */
  editing: { conversationId: string; messageId: string; text: string } | null;
  /** Uploaded and waiting to be sent with the next message. */
  pendingAttachments: Attachment[];
  /** Names of files currently uploading, so the composer can show them. */
  uploadingAttachments: string[];
  /** Desktop notification when a run finishes while this tab is hidden (per device). */
  notifyRuns: boolean;
}

export interface Actions {
  boot: () => void;
  applyEvents: (events: ServerEvent[]) => void;
  setView: (view: View) => void;
  openConversation: (id: string | null, opts?: { replace?: boolean; silent?: boolean }) => Promise<void>;
  newChat: () => void;
  refreshConversation: (id: string) => Promise<void>;
  /** True when the message was handed to the socket. False means it was refused and the
   *  composer must KEEP the text — clearing it unconditionally lost what Arsen had typed
   *  whenever the connection had dropped or a run was still going. */
  send: (text: string) => boolean;
  stop: () => void;
  confirmTool: (runId: string, callId: string, approved: boolean, note?: string) => void;
  renameConversation: (id: string, title: string) => Promise<void>;
  archiveConversation: (id: string, archived: boolean) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  loadArchived: () => Promise<void>;
  loadRunEvents: (runId: string) => Promise<void>;
  openInspector: (runId: string | null) => void;
  setSidebarOpen: (open: boolean) => void;
  setTheme: (pref: ThemePref) => void;
  setThinkChoice: (choice: ThinkChoice) => void;
  setMoreOpen: (open: boolean) => void;
  notify: (text: string, level?: Notice['level']) => void;
  dismissNotice: () => void;
  pinConversation: (id: string, pinned: boolean) => Promise<void>;
  /** Re-run the last user message of the open conversation; the earlier reply stays. */
  regenerate: () => void;
  startEdit: (conversationId: string, messageId: string, text: string) => void;
  cancelEdit: () => void;
  /** Fork the conversation before the edited message and send the new text there. False when
   *  the fork or the send was refused, so the composer keeps the edit. */
  sendEdit: (text: string) => Promise<boolean>;
  setNotifyRuns: (on: boolean) => Promise<void>;
  /** Upload files (photos, documents) and hold them for the next message. */
  attachFiles: (files: File[]) => Promise<void>;
  attachText: (text: string, name?: string) => Promise<void>;
  removeAttachment: (id: string) => void;
  exportConversation: (id: string, format: 'markdown' | 'json') => Promise<void>;
}

export type AppState = ChatState & FeatureState & UiState & Actions;

let ws: WsClient | null = null;
let noticeSeq = 0;
let clientRefSeq = 0;

function errorText(e: unknown): string {
  if (e instanceof Error && 'status' in e) return describeError((e as { status: number }).status, (e as { body?: unknown }).body);
  return e instanceof Error ? e.message : String(e);
}

export const useStore = create<AppState>()((set, get) => ({
  ...initialChatState(),
  ...initialFeatureState(),
  view: 'chat',
  connection: 'connecting',
  connectionAttempt: 0,
  sidebarOpen: false,
  inspectorRunId: null,
  conversationsLoaded: false,
  archivedLoaded: false,
  loadingMessagesFor: null,
  runEventsLoaded: {},
  notice: null,
  themePref: readThemePref(),
  panelMode: isPanelMode(),
  version: null,
  thinkChoice: {},
  summaries: {},
  moreOpen: false,
  editing: null,
  pendingAttachments: [],
  uploadingAttachments: [],
  notifyRuns: readNotifyPref(),

  boot: () => {
    const route = parseLocation();
    set({ view: route.view });
    applyThemePref(get().themePref);

    ws = new WsClient({
      onEvents: (events) => get().applyEvents(events),
      onState: (connection, connectionAttempt) => set({ connection, connectionAttempt }),
      onOpen: (isReconnect) => {
        const open = get().openConversationId;
        if (open) {
          ws?.send({ type: 'subscribe', conversation_id: open });
          if (isReconnect) void get().refreshConversation(open);
        }
        if (isReconnect) void loadConversations(set, get);
      },
    });
    ws.connect();

    void loadConversations(set, get);
    api.health()
      .then((h) => set({ version: h.version }))
      .catch(() => undefined);

    if (route.conversationId) void get().openConversation(route.conversationId, { replace: true });

    window.addEventListener('popstate', () => {
      const r = parseLocation();
      set({ view: r.view, sidebarOpen: false });
      if (r.conversationId !== get().openConversationId) void get().openConversation(r.conversationId, { silent: true });
    });
  },

  applyEvents: (events) => {
    const before = get();
    const after = applyFeatureEvents(applyServerEvents(before, events, Date.now()), events) as AppState;
    set(after);
    // A conversation the server created for our null-conversation send: subscribe and route to it.
    if (after.openConversationId && after.openConversationId !== before.openConversationId) {
      const id = after.openConversationId;
      ws?.send({ type: 'subscribe', conversation_id: id });
      rememberConversation(id);
      navigate(after.view, id, true);
      const pendingChoice = after.thinkChoice[NEW_CONVERSATION_KEY];
      if (pendingChoice) set((s) => ({ thinkChoice: { ...omit(s.thinkChoice, NEW_CONVERSATION_KEY), [id]: pendingChoice } }));
    }
    // The server flags a conversation unread when a run ends; the one on screen is read.
    const open = after.openConversationId;
    if (open && events.some((e) => e.type === 'conversation.updated' && e.conversation.id === open && e.conversation.unread)) {
      api.conversations.patch(open, { unread: false }).catch(() => undefined);
    }
    // A long task finished while Arsen was elsewhere: one desktop notification, if he opted in.
    if (after.notifyRuns && document.visibilityState === 'hidden') {
      for (const e of events) {
        if (e.type === 'run.done' || e.type === 'run.failed') {
          const conv = after.conversations[e.conversation_id];
          notifyDesktop(conv?.title ?? 'Jarvis', e.type === 'run.done' ? (conv?.preview ?? 'Finished.') : `Failed: ${e.error}`, e.conversation_id);
        }
      }
    }
  },

  setView: (view) => {
    set({ view, sidebarOpen: false, moreOpen: false });
    navigate(view, get().openConversationId);
  },

  setMoreOpen: (open) => set({ moreOpen: open }),

  openConversation: async (id, opts = {}) => {
    const prev = get().openConversationId;
    if (prev && prev !== id) ws?.send({ type: 'unsubscribe', conversation_id: prev });
    set({ openConversationId: id, view: 'chat', sidebarOpen: false, pendingNewConversation: null });
    rememberConversation(id);
    if (!opts.silent) navigate('chat', id, opts.replace ?? false);
    if (!id) return;
    ws?.send({ type: 'subscribe', conversation_id: id });
    const conv = get().conversations[id];
    if (conv?.unread) {
      set((s) => ({ conversations: { ...s.conversations, [id]: { ...conv, unread: false } } }));
      api.conversations.patch(id, { unread: false }).catch(() => undefined);
    }
    await get().refreshConversation(id);
  },

  newChat: () => {
    const prev = get().openConversationId;
    if (prev) ws?.send({ type: 'unsubscribe', conversation_id: prev });
    set((s) => ({
      openConversationId: null,
      view: 'chat',
      sidebarOpen: false,
      pendingNewConversation: null,
      messages: omit(s.messages, NEW_CONVERSATION_KEY),
    }));
    rememberConversation(null);
    navigate('chat', null);
  },

  refreshConversation: async (id) => {
    set({ loadingMessagesFor: id });
    try {
      const [conv, serverMessages, serverRuns, summary] = await Promise.all([
        get().conversations[id] ? Promise.resolve(null) : api.conversations.get(id),
        api.conversations.messages(id),
        api.conversations.runs(id),
        api.conversations.summary(id).catch(() => null),
      ]);
      set((s) => ({ ...mergeConversationData(s, id, conv, serverMessages, serverRuns), summaries: { ...s.summaries, [id]: summary } }));
      for (const run of serverRuns) {
        if (!isTerminal(run.status)) void get().loadRunEvents(run.id);
      }
    } catch (e) {
      get().notify(`Could not load conversation: ${errorText(e)}`, 'error');
    } finally {
      if (get().loadingMessagesFor === id) set({ loadingMessagesFor: null });
    }
  },

  send: (text) => {
    const s = get();
    const trimmed = text.trim();
    const attachments = s.pendingAttachments;
    const open = s.openConversationId;
    const socket = ws;
    // A photo with no words is a perfectly good message ("what is this?" is implied).
    const refusal = selectSendRefusal(s, {
      connection: socket?.isOpen ? 'open' : 'closed', // the socket itself, not the last reported state
      hasText: Boolean(trimmed) || attachments.length > 0,
      conversationId: open,
    });
    if (refusal !== null || !socket) {
      if (refusal !== null && refusal !== 'nothing to send') s.notify(refusal, 'error');
      return false;
    }
    const clientRef = `c${Date.now().toString(36)}_${(clientRefSeq++).toString(36)}`;
    const optimistic: LocalMessage = {
      id: `local_${clientRef}`,
      conversation_id: open,
      run_id: null,
      role: 'user',
      content: trimmed,
      reasoning: null,
      tool_calls: [],
      tool_call_id: null,
      name: null,
      partial: false,
      attachments,
      created_at: new Date().toISOString(),
      optimistic: true,
    };
    const key = open ?? NEW_CONVERSATION_KEY;
    set((st) => ({
      messages: { ...st.messages, [key]: [...(open ? (st.messages[key] ?? []) : []), optimistic] },
      pendingNewConversation: open ? st.pendingNewConversation : { clientRef, text: trimmed },
      pendingAttachments: [],
    }));
    const choice = s.thinkChoice[key];
    return socket.send({
      type: 'run.create',
      conversation_id: open,
      text: trimmed,
      kind: 'chat',
      client_ref: clientRef,
      think: choice?.think ?? null,
      think_level: choice?.think ? (choice.think_level ?? null) : null,
      attachment_ids: attachments.map((a) => a.id),
    });
  },

  stop: () => {
    const s = get();
    const run = selectActiveRun(s, s.openConversationId);
    if (!run) return;
    if (ws?.send({ type: 'run.cancel', run_id: run.id })) {
      set({ cancelRequested: { ...s.cancelRequested, [run.id]: true } });
    } else {
      s.notify('Not connected — could not send Stop.', 'error');
    }
  },

  confirmTool: (runId, callId, approved, note) => {
    const trimmed = note?.trim() ?? '';
    if (!ws?.send({ type: 'tool.confirm', run_id: runId, call_id: callId, approved, note: trimmed.length > 0 ? trimmed : null })) {
      get().notify('Not connected — could not send the decision.', 'error');
    }
  },

  renameConversation: async (id, title) => {
    try {
      const conv = await api.conversations.patch(id, { title });
      upsertConversation(set, conv);
    } catch (e) {
      get().notify(`Rename failed: ${errorText(e)}`, 'error');
    }
  },

  pinConversation: async (id, pinned) => {
    try {
      const conv = await api.conversations.patch(id, { pinned });
      upsertConversation(set, conv);
    } catch (e) {
      get().notify(`${pinned ? 'Pin' : 'Unpin'} failed: ${errorText(e)}`, 'error');
    }
  },

  regenerate: () => {
    const s = get();
    const open = s.openConversationId;
    if (!open || selectActiveRun(s, open)) return;
    const last = [...(s.messages[open] ?? [])].reverse().find((m) => m.role === 'user' && !m.name && !m.optimistic);
    if (!last) return;
    s.send(last.content);
  },

  startEdit: (conversationId, messageId, text) => {
    set({ editing: { conversationId, messageId, text } });
    window.dispatchEvent(new CustomEvent('jarvis:compose', { detail: text }));
  },
  cancelEdit: () => set({ editing: null }),

  sendEdit: async (text) => {
    const s = get();
    const editing = s.editing;
    const trimmed = text.trim();
    if (!editing || !trimmed) return false;
    try {
      const fork = await api.conversations.fork(editing.conversationId, editing.messageId);
      set({ editing: null });
      upsertConversation(set, fork);
      await get().openConversation(fork.id);
      return get().send(trimmed);
    } catch (e) {
      get().notify(`Could not fork the conversation: ${errorText(e)}`, 'error');
      return false;
    }
  },

  attachFiles: async (files) => {
    for (const file of files) {
      set((s) => ({ uploadingAttachments: [...s.uploadingAttachments, file.name] }));
      try {
        const att = await api.attachments.upload(file, get().openConversationId);
        set((s) => ({ pendingAttachments: [...s.pendingAttachments, att] }));
      } catch (e) {
        get().notify(`${file.name}: ${errorText(e)}`, 'error');
      } finally {
        set((s) => ({ uploadingAttachments: s.uploadingAttachments.filter((n) => n !== file.name) }));
      }
    }
  },

  attachText: async (text, name = 'pasted text') => {
    try {
      const att = await api.attachments.uploadText(text, name, get().openConversationId);
      set((s) => ({ pendingAttachments: [...s.pendingAttachments, att] }));
    } catch (e) {
      get().notify(`Could not attach the text: ${errorText(e)}`, 'error');
    }
  },

  removeAttachment: (id) => {
    set((s) => ({ pendingAttachments: s.pendingAttachments.filter((a) => a.id !== id) }));
    api.attachments.remove(id).catch(() => undefined); // best effort: the row is orphaned anyway
  },

  setNotifyRuns: async (on) => {
    if (on && 'Notification' in window && Notification.permission === 'default') {
      const result = await Notification.requestPermission();
      if (result !== 'granted') {
        get().notify('Notifications are blocked by the browser for this site.', 'error');
        return;
      }
    }
    if (on && (!('Notification' in window) || Notification.permission !== 'granted')) {
      get().notify('Notifications are not available in this browser.', 'error');
      return;
    }
    writeNotifyPref(on);
    set({ notifyRuns: on });
  },

  exportConversation: async (id, format) => {
    const s = get();
    const conv = s.conversations[id];
    if (!conv) return;
    try {
      const messages = s.messages[id] ?? (await api.conversations.messages(id));
      const stamp = conv.updated_at.slice(0, 10);
      const base = `${safeFilename(conv.title)}-${stamp}`;
      if (format === 'json') downloadText(`${base}.json`, JSON.stringify({ conversation: conv, messages }, null, 2), 'application/json');
      else downloadText(`${base}.md`, conversationToMarkdown(conv, messages), 'text/markdown');
    } catch (e) {
      get().notify(`Export failed: ${errorText(e)}`, 'error');
    }
  },

  archiveConversation: async (id, archived) => {
    try {
      const conv = await api.conversations.patch(id, { archived });
      upsertConversation(set, conv);
    } catch (e) {
      get().notify(`${archived ? 'Archive' : 'Unarchive'} failed: ${errorText(e)}`, 'error');
    }
  },

  deleteConversation: async (id) => {
    try {
      await api.conversations.remove(id);
      get().applyEvents([{ type: 'conversation.deleted', ts: new Date().toISOString(), conversation_id: id }]);
      if (get().openConversationId === null) navigate('chat', null, true);
    } catch (e) {
      get().notify(`Delete failed: ${errorText(e)}`, 'error');
    }
  },

  loadArchived: async () => {
    if (get().archivedLoaded) return;
    try {
      const list = await api.conversations.list(true);
      set((s) => ({ conversations: indexBy(list, s.conversations), archivedLoaded: true }));
    } catch (e) {
      get().notify(`Could not load the archive: ${errorText(e)}`, 'error');
    }
  },

  loadRunEvents: async (runId) => {
    try {
      const [run, events] = await Promise.all([api.runs.get(runId).catch(() => null), api.runs.events(runId, 0)]);
      set((s) => {
        const existing = s.runEvents[runId] ?? [];
        const persisted = events.filter(isRunScoped).filter((e) => e.type !== 'model.delta');
        const seqs = new Set(persisted.map((e) => e.seq));
        const merged: RunScopedEvent[] = [...persisted, ...existing.filter((e) => e.seq === 0 || !seqs.has(e.seq))];
        const runs = run ? { ...s.runs, [runId]: reconcileRun(s.runs[runId], run) } : s.runs;
        return { runEvents: { ...s.runEvents, [runId]: merged }, runEventsLoaded: { ...s.runEventsLoaded, [runId]: true }, runs };
      });
    } catch (e) {
      get().notify(`Could not load run events: ${errorText(e)}`, 'error');
    }
  },

  openInspector: (runId) => {
    set({ inspectorRunId: runId });
    if (runId && !get().runEventsLoaded[runId]) void get().loadRunEvents(runId);
  },

  setSidebarOpen: (open) => set({ sidebarOpen: open }),

  setTheme: (pref) => {
    applyThemePref(pref);
    set({ themePref: pref });
  },

  setThinkChoice: (choice) => {
    const key = get().openConversationId ?? NEW_CONVERSATION_KEY;
    set((s) => ({ thinkChoice: choice.think === null ? omit(s.thinkChoice, key) : { ...s.thinkChoice, [key]: choice } }));
  },

  notify: (text, level = 'info') => set({ notice: { id: ++noticeSeq, level, text } }),
  dismissNotice: () => set({ notice: null }),
}));

// ---- helpers -----------------------------------------------------------------------------

type Set = (partial: Partial<AppState> | ((s: AppState) => Partial<AppState>)) => void;
type Get = () => AppState;

async function loadConversations(set: Set, get: Get): Promise<void> {
  try {
    const list = await api.conversations.list(false);
    set((s) => ({ conversations: indexBy(list, s.conversations), conversationsLoaded: true }));
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      get().notify('The core rejected the token. Open the app with ?token=… from the core’s pairing link.', 'error');
      return;
    }
    get().notify(`Could not load conversations: ${errorText(e)}`, 'error');
  }
}

function indexBy(list: Conversation[], into: Record<string, Conversation>): Record<string, Conversation> {
  const out = { ...into };
  for (const c of list) out[c.id] = c;
  return out;
}

function upsertConversation(set: Set, conv: Conversation): void {
  set((s) => ({ conversations: { ...s.conversations, [conv.id]: conv } }));
}

/** A live head (from events) may be ahead of a REST snapshot fetched a moment earlier. */
function reconcileRun(local: Run | undefined, fetched: Run): Run {
  if (!local) return fetched;
  if (isTerminal(local.status) && !isTerminal(fetched.status)) return { ...fetched, status: local.status, finished_at: local.finished_at, error: local.error };
  return fetched;
}

function mergeConversationData(
  s: AppState,
  id: string,
  conv: Conversation | null,
  serverMessages: Message[],
  serverRuns: Run[],
): Partial<AppState> {
  const ids = new Set(serverMessages.map((m) => m.id));
  const localOnly = (s.messages[id] ?? []).filter((m) => m.optimistic === true || (m.id !== null && !ids.has(m.id) && m.partial));
  const messages: LocalMessage[] = [...serverMessages, ...localOnly];

  const runs = { ...s.runs };
  const finished: string[] = [];
  for (const r of serverRuns) {
    const head = reconcileRun(s.runs[r.id], r);
    runs[r.id] = head;
    if (isTerminal(head.status)) finished.push(r.id);
  }
  const streams = omit(s.streams, ...finished);
  const order = serverRuns.map((r) => r.id);
  for (const rid of s.runsByConversation[id] ?? []) if (!order.includes(rid)) order.unshift(rid);

  const conversations = conv ? { ...s.conversations, [conv.id]: conv } : s.conversations;
  return {
    conversations,
    messages: { ...s.messages, [id]: messages },
    runs,
    streams,
    runsByConversation: { ...s.runsByConversation, [id]: order },
  };
}
