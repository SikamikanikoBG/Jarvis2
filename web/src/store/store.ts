import { create } from 'zustand';
import { ApiError, api, describeError } from '../api/client';
import { WsClient, type ConnectionState } from '../api/ws';
import { conversationToMarkdown, downloadText, safeFilename } from '../lib/export';
import { readCollapsedFolders, writeCollapsedFolders } from '../lib/folders';
import { notifyDesktop, readNotifyPref, writeNotifyPref } from '../lib/notify';
import { navigate, parseLocation, rememberConversation, takeRing, type IncomingRing, type View } from '../lib/router';
import { applyThemePref, isPanelMode, readThemePref, type ThemePref } from '../lib/theme';
import { DRAFT_NORMAL, ttlLabel, type DraftPrivacy } from '../lib/privacy';
import type { Annotation } from '../lib/annotations';
import type { ThinkChoice } from '../lib/think';
import { enablePush, pushSupported, refreshPush } from '../push/push';
import { MicListener } from '../voice/listener';
import { requestMotionPermission } from '../voice/earPose';
import { holdScreen, releaseScreen } from '../voice/screenHold';
import { firstSentences } from '../voice/sentences';
import { CallSession, type CallRoute, type CallState } from '../voice/session';
import { CallLife } from '../voice/callLife';
import { ToneCues, type WaitingSound } from '../voice/tones';
import { DeviceSpeaker, ServerSpeaker } from '../voice/speaker';
import { LineCues, LineSpeaker, VoiceLine } from '../voice/line';
import { callTrace } from '../voice/trace';
import type {
  Attachment,
  BulkConversationAction,
  ChatFolder,
  Conversation,
  ConversationSummary,
  Message,
  Run,
  SessionRef,
  RunScopedEvent,
  ServerEvent,
  Task,
} from '../protocol/types';
import { isRunScoped, isTerminal } from '../protocol/types';
import { applyFeatureEvents, initialFeatureState, type FeatureState } from './features';
import { applyServerEvents, taskChanged } from './reducer';
import { selectActiveRun, selectIncognitoNow, selectSendRefusal, selectUnreadIds } from './selectors';
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
  /**
   * How the NEXT new chat opens: normal, incognito, or disappearing after an idle time. Picked
   * in the composer before the first message and sent with it — an incognito chat has to be one
   * from its very first word. Reset once that chat exists.
   */
  draftPrivacy: DraftPrivacy;
  /**
   * Arsen asked for a new chat (the + button, the shortcut): the call button then talks in that
   * new chat, not in the Jarvis home line. Off again once any conversation opens.
   */
  freshChat: boolean;
  /** Compaction summary per conversation; `undefined` = not fetched, `null` = none. */
  summaries: Record<string, ConversationSummary | null | undefined>;
  /** Mobile "More" navigation sheet. */
  moreOpen: boolean;
  /** "Edit and resend": the message being edited; sending forks the conversation before it. */
  editing: { conversationId: string; messageId: string; text: string } | null;
  /** Comments on passages of replies, per conversation, waiting to go out with the next message. */
  annotations: Record<string, Annotation[] | undefined>;
  /** Uploaded and waiting to be sent with the next message. */
  pendingAttachments: Attachment[];
  /** Names of files currently uploading, so the composer can show them. */
  uploadingAttachments: string[];
  /** Desktop notification when a run finishes while this tab is hidden (per device). */
  notifyRuns: boolean;
  /** Arsen's own chat folders, in their order. */
  folders: ChatFolder[];
  /** The chats as addressable sessions (@handle), for the composer's "@" menu. */
  sessions: SessionRef[];
  /** The call in progress (docs/stories/10_voice.md), or null. A pure function of the session. */
  call: CallState | null;
  /** The live call's id (voice/callLife.ts): what the call screen's noted list is about. */
  callId: string | null;
  /** The call's sound comes over the line (voice/line.ts), not from the page. */
  callLine: boolean;
  /** Jarvis ringing: a ring opened from its notification, waiting to be answered or declined. */
  incoming: IncomingRing | null;
  /**
   * The bell for the NEXT message, per chat, when he has touched it (docs/stories/11_call_me.md).
   * Untouched, `callMeDefault` decides. A run already working carries its own flag (Run.call_me).
   */
  callMeNext: Record<string, boolean | undefined>;
  /** Whether a chat rings by default when its task is done (settings.call_me_default). */
  callMeDefault: boolean;
  /** settings.tasks.approval_ttl_s, for "expires at" on a task's approval; null = not known. */
  approvalTtlS: number | null;
  /** settings.tasks.enabled: the new-chat hint says Jarvis can take work as a task. */
  tasksEnabled: boolean;
  /** Sidebar multi-select: the chosen conversation ids. Empty means not selecting anything. */
  selection: string[];
  /** The row a shift-click measures its range from. */
  selectionAnchor: string | null;
  /** Folder ids collapsed by hand (per device). */
  collapsedFolders: string[];
  /**
   * Sidebar filter: only the chats that want him — a run going, or an unread reply.
   *
   * Unread belongs in the same view as running because they are the same chat five seconds
   * apart: a run finishes, the blue dot appears, and having to leave the filter to go and read
   * the answer was the whole point of turning it on.
   *
   * Session-only on purpose — it is a "what is waiting for me" view, not a preference.
   * Remembered across a reload it would greet him with an empty list on a quiet morning and
   * look like every chat had vanished.
   */
  attentionOnly: boolean;
  /**
   * A message the transcript should scroll to and flash once it has rendered — how a search hit
   * inside a long conversation lands on the line that matched instead of at the bottom.
   * Cleared by the transcript the moment it finds it.
   */
  scrollToMessage: string | null;
}

export interface Actions {
  boot: () => void;
  applyEvents: (events: ServerEvent[]) => void;
  setView: (view: View) => void;
  /** `stay` keeps the current screen and URL: the mail desk opens a thread's chat beside it. */
  openConversation: (id: string | null, opts?: { replace?: boolean; silent?: boolean; stay?: boolean }) => Promise<void>;
  newChat: () => void;
  refreshConversation: (id: string) => Promise<void>;
  /** True when the message was handed to the socket. False means it was refused and the
   *  composer must KEEP the text — clearing it unconditionally lost what Arsen had typed
   *  whenever the connection had dropped or a run was still going. */
  send: (text: string) => boolean;
  stop: () => void;
  confirmTool: (runId: string, callId: string, approved: boolean, note?: string) => void;
  /** Delegated tasks from their card (docs/stories/13_delegated_tasks.md). */
  cancelTask: (taskId: string) => void;
  /** An answer to what the task asked, or a change of mind: into it, or a new round of it. */
  steerTask: (taskId: string, text: string) => boolean;
  /** Approve the call a task waits on and never ask for that tool in tasks again. */
  allowToolInTasks: (taskId: string, callId: string) => void;
  /** The task's own conversation: its brief, every step, the full result. */
  openTaskWork: (task: Task) => void;
  renameConversation: (id: string, title: string) => Promise<void>;
  archiveConversation: (id: string, archived: boolean) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  loadArchived: () => Promise<void>;
  loadRunEvents: (runId: string) => Promise<void>;
  openInspector: (runId: string | null) => void;
  setSidebarOpen: (open: boolean) => void;
  setTheme: (pref: ThemePref) => void;
  setThinkChoice: (choice: ThinkChoice) => void;
  setDraftPrivacy: (privacy: DraftPrivacy) => void;
  /** How long an existing chat may sit idle before it deletes itself; `null` keeps it. */
  setConversationTtl: (id: string, ttlSeconds: number | null) => Promise<void>;
  setMoreOpen: (open: boolean) => void;
  /** Start a call in the open conversation (or a new chat). Must be called from a user gesture:
   *  the microphone and the first spoken word both need one. */
  /** `page`: the voice is played by the page, never the line - the old way, one tap away. */
  startCall: (opts?: { say?: string; page?: boolean; route?: CallRoute }) => Promise<void>;
  endCall: () => void;
  /** A ring arrived (its notification was tapped): open its chat and show the incoming call -
   *  or, when Answer was tapped on the notification, go straight into the call if the page may. */
  receiveRing: (ring: IncomingRing) => void;
  /** Answer the incoming call - from a tap, like any call: it opens and says the result first. */
  answerRing: () => Promise<void>;
  declineRing: () => void;
  /**
   * The bell: ring when this is done. With a run working in the open chat it is that run's;
   * otherwise it goes with the next message. Called from a tap, so the first one can also ask
   * this device for permission to ring.
   */
  toggleCallMe: (opts?: { voice?: boolean }) => void;
  setCallMeDefault: (on: boolean) => void;
  setCallMuted: (muted: boolean) => void;
  /** Earpiece or speaker; remembered on this device for the next call. */
  setCallRoute: (route: CallRoute) => void;
  notify: (text: string, level?: Notice['level']) => void;
  dismissNotice: () => void;
  pinConversation: (id: string, pinned: boolean) => Promise<void>;
  /** A persona or standing rule for ONE chat; "" clears it. */
  setConversationInstructions: (id: string, instructions: string) => Promise<void>;
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
  /** True once the text is held for the next message. */
  attachText: (text: string, name?: string) => Promise<boolean>;
  removeAttachment: (id: string) => void;
  /** A comment on a passage of a reply (lib/annotations.ts), held for the next message. */
  addAnnotation: (conversationId: string, note: Annotation) => void;
  removeAnnotation: (conversationId: string, id: string) => void;
  clearAnnotations: (conversationId: string) => void;
  exportConversation: (id: string, format: 'markdown' | 'json') => Promise<void>;
  loadFolders: () => Promise<void>;
  /** The @handles, fetched when an "@" is typed (the core derives them from the titles). */
  loadSessions: () => Promise<void>;
  /** Creates the folder and returns its id, or null when the server refused. */
  createFolder: (name: string) => Promise<string | null>;
  renameFolder: (id: string, name: string) => Promise<void>;
  /** Removes the folder; the chats in it fall back into the flat list. */
  deleteFolder: (id: string) => Promise<void>;
  moveConversation: (id: string, folderId: string | null) => Promise<void>;
  toggleFolderCollapsed: (id: string) => void;
  setAttentionOnly: (on: boolean) => void;
  /** Open a conversation and land on one of its messages (a search hit). */
  openConversationAt: (conversationId: string, messageId: string | null) => Promise<void>;
  clearScrollToMessage: () => void;
  /** Add or remove one row from the sidebar selection (and make it the range anchor). */
  toggleSelected: (id: string) => void;
  /** Shift-click: select everything between the anchor and `id` along the rows as shown. */
  extendSelection: (id: string, ordered: string[]) => void;
  setSelection: (ids: string[]) => void;
  clearSelection: () => void;
  /** One action over the whole selection, in one request. */
  bulkSelected: (action: BulkConversationAction, folderId?: string | null) => Promise<void>;
  /** Mark every unread chat read in one request. */
  markAllRead: () => Promise<void>;
}

export type AppState = ChatState & FeatureState & UiState & Actions;

let ws: WsClient | null = null;
let noticeSeq = 0;
let clientRefSeq = 0;
/** The call in progress. One at a time; the store's `call` mirrors its state. */
let session: CallSession | null = null;
/** A call is being set up (settings, home chat) and has no session yet. */
let callStarting = false;
let speaker: DeviceSpeaker | ServerSpeaker | LineSpeaker | null = null;
/** The call's line, when its sound goes over one (voice/line.ts). */
let line: VoiceLine | null = null;
/** The call as the core sees it: its id, the heartbeat, the hang-up (voice/callLife.ts). */
let callLife: CallLife | null = null;

/** Where the phone is (Europe/Berlin on a trip), so the turn is told the local time there. */
function deviceTimezone(): { timezone?: string } {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return tz ? { timezone: tz } : {};
  } catch {
    return {};
  }
}

const ROUTE_KEY = 'jarvis_call_route';
function readRoute(): CallRoute {
  try {
    return localStorage.getItem(ROUTE_KEY) === 'speaker' ? 'speaker' : 'earpiece';
  } catch {
    return 'earpiece';
  }
}
function writeRoute(route: CallRoute): void {
  try {
    localStorage.setItem(ROUTE_KEY, route);
  } catch {
    /* private mode: the choice lasts the session */
  }
}

/** Past tense for the toast after a bulk action. */
const BULK_DONE: Record<BulkConversationAction, string> = {
  delete: 'Deleted',
  archive: 'Archived',
  unarchive: 'Unarchived',
  move: 'Moved',
  read: 'Marked read',
  pin: 'Pinned',
  unpin: 'Unpinned',
};

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
  call: null,
  callId: null,
  callLine: false,
  incoming: null,
  callMeNext: {},
  callMeDefault: true,
  approvalTtlS: null,
  tasksEnabled: false,
  thinkChoice: {},
  draftPrivacy: DRAFT_NORMAL,
  freshChat: false,
  summaries: {},
  moreOpen: false,
  editing: null,
  annotations: {},
  pendingAttachments: [],
  uploadingAttachments: [],
  notifyRuns: readNotifyPref(),
  folders: [],
  sessions: [],
  selection: [],
  selectionAnchor: null,
  collapsedFolders: readCollapsedFolders(),
  attentionOnly: false,
  scrollToMessage: null,

  boot: () => {
    const route = parseLocation();
    set({ view: route.view });
    applyThemePref(get().themePref);

    ws = new WsClient({
      onEvents: (events) => get().applyEvents(events),
      onState: (connection, connectionAttempt) => {
        set({ connection, connectionAttempt });
        session?.setOnline(connection === 'open');
      },
      onOpen: (isReconnect) => {
        sendPresence();
        const open = get().openConversationId;
        if (open) {
          ws?.send({ type: 'subscribe', conversation_id: open });
          if (isReconnect) void get().refreshConversation(open);
        }
        if (isReconnect) {
          void loadConversations(set, get);
          void get().loadFolders();
          void loadOpenTasks(set);
          // Still on the call: a reconnect is not a hang-up, and the core hears it at once.
          callLife?.alive();
        }
      },
    });
    ws.connect();
    // Back from sleep, a background tab or a network change: events may have been missed on a
    // socket that looked open. Reload the list now (a chat that turned unread must show at once)
    // and let the socket reopen itself if it has been silent.
    const wake = () => {
      ws?.revive();
      void loadConversations(set, get);
    };
    document.addEventListener('visibilitychange', () => {
      sendPresence();
      if (document.visibilityState === 'visible') wake();
    });
    window.addEventListener('online', wake);
    // The page is going away with a call on it (closed, navigated off): that is a hang-up.
    window.addEventListener('pagehide', (e) => {
      if (!e.persisted) callLife?.end(true);
    });

    void loadConversations(set, get);
    void get().loadFolders();
    void loadOpenTasks(set);
    api.health()
      .then((h) => set({ version: h.version }))
      .catch(() => undefined);

    const ring = takeRing();
    // A reload on another view (Settings, Runs) keeps that view: the remembered chat opens behind it.
    if (route.conversationId) void get().openConversation(route.conversationId, { replace: true, stay: route.view !== 'chat' });
    // Jarvis rang and the notification was tapped: the app opened on it (an address) or, when it
    // was already open, the service worker says so.
    if (ring) get().receiveRing(ring);
    navigator.serviceWorker?.addEventListener('message', (e: MessageEvent<{ type?: string; path?: string; say?: string; answered?: boolean }>) => {
      // A notice was tapped (an important mail, the triage audit) while the app was open: go there.
      if (e.data?.type === 'jarvis.navigate' && e.data.path) {
        const target = parseLocation(new URL(e.data.path, window.location.origin).pathname);
        if (target.conversationId) void get().openConversation(target.conversationId);
        else get().setView(target.view);
        return;
      }
      if (e.data?.type !== 'jarvis.ring' || !e.data.path) return;
      const url = new URL(e.data.path, window.location.origin);
      if (e.data.say) url.searchParams.set('say', e.data.say);
      if (e.data.answered) url.searchParams.set('answer', '1');
      const r = takeRing(url.href);
      if (r) get().receiveRing(r);
    });
    void refreshPush().catch(() => undefined);
    api.settings
      .get()
      .then((st) => set({ callMeDefault: st.call_me_default.includes('chat'), approvalTtlS: st.tasks?.approval_ttl_s ?? null, tasksEnabled: st.tasks?.enabled ?? false }))
      .catch(() => undefined);

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
    // A call in progress hears the run it is answering: the queue, the words, the end.
    if (session) {
      for (const e of events) {
        if (e.type === 'run.queued') session.onRunQueued(e.run_id, e.conversation_id);
        else if (e.type === 'model.delta' && e.kind === 'text') session.onDelta(e.run_id, e.text);
        else if (e.type === 'model.done') session.onStepDone(e.run_id, e.tool_call_count > 0 || e.finish_reason === 'tool_calls');
        else if (e.type === 'run.done' || e.type === 'run.cancelled') session.onRunDone(e.run_id);
        else if (e.type === 'run.failed') session.onRunFailed(e.run_id, e.error);
      }
    }
    // A conversation the server created for our null-conversation send: subscribe and route to it.
    if (after.openConversationId && after.openConversationId !== before.openConversationId) {
      const id = after.openConversationId;
      callLife?.setConversation(id);
      set({ freshChat: false });
      ws?.send({ type: 'subscribe', conversation_id: id });
      rememberConversation(id);
      navigate(after.view, id, true);
      const pendingChoice = after.thinkChoice[NEW_CONVERSATION_KEY];
      if (pendingChoice) set((s) => ({ thinkChoice: { ...omit(s.thinkChoice, NEW_CONVERSATION_KEY), [id]: pendingChoice } }));
      // The draft's privacy went out with the first message; the chat now says it itself.
      if (before.pendingNewConversation) set({ draftPrivacy: DRAFT_NORMAL });
    }
    // Folders are Arsen's own filing and the sidebar always shows them: refetch rather than
    // reduce, so a folder made on the phone (and its counts) appear here without a reload.
    if (events.some((e) => e.type === 'folders.changed')) void get().loadFolders();
    // A chat deleted anywhere (another tab, a bulk action) must not stay in this selection.
    const gone = events.filter((e) => e.type === 'conversation.deleted').map((e) => e.conversation_id);
    if (gone.length > 0 && after.selection.some((id) => gone.includes(id))) {
      set({ selection: after.selection.filter((id) => !gone.includes(id)) });
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

  startCall: async (opts) => {
    // The session exists only after the settings and the home chat are fetched: until then a
    // second tap on the call button started a second call, with a microphone of its own.
    if (session || callStarting) return;
    const s = get();
    let open = s.openConversationId;
    const socket = ws;
    if (!socket?.isOpen) {
      s.notify('Not connected to Jarvis.', 'error');
      return;
    }
    callStarting = true;
    // Still inside the tap, before anything is awaited: fullscreen and portrait for the call,
    // and (iOS) leave to read the accelerometer that tells an ear from a look.
    holdScreen();
    void requestMotionPermission();
    // Whose voice: the core's neural one unless the settings say the device's. Read here, at
    // the tap, so a change in Settings takes effect on the next call without a reload.
    let tts: 'server' | 'device' = 'server';
    let languages = ['bg', 'en'];
    let turnThreshold: number | null = null;
    let waiting: WaitingSound = 'music';
    let holdVolume = 0.4;
    let useLine = !opts?.page;
    let debug = false;
    try {
      const settings = await api.settings.get();
      tts = settings.voice.tts;
      if (settings.stt_languages.length) languages = settings.stt_languages;
      if (settings.voice.turn_detection) turnThreshold = settings.voice.turn_threshold;
      waiting = settings.voice.waiting_sound;
      holdVolume = settings.voice.hold_volume;
      useLine = useLine && settings.voice.line;
      debug = settings.voice_debug;
    } catch {
      /* the defaults stand; the server voice falls back to the device on its own */
    }
    // An incognito call is one from the first word: the chat it opens is incognito, and the
    // sentences Jarvis says are not written to the core's voice cache.
    const incognito = selectIncognitoNow(s);
    // No chat open: the call is to Jarvis himself, in the one conversation that is about the day
    // rather than a task (docs/stories/12_assistant.md). If it cannot be had, a new chat as before.
    // Not after "New chat": that asked for an empty chat, and the call starts it.
    if (!open && !incognito && !s.freshChat) {
      try {
        const home = await api.assistant.home();
        await get().openConversation(home.id);
        open = home.id;
      } catch {
        /* a new chat opens behind the call, the way it always did */
      }
    }
    // The line: his voice and the music as a real call's remote track (voice/line.ts). It comes
    // up beside the microphone; if it cannot, the page plays them itself, as before.
    const onLine = useLine && tts === 'server' && typeof RTCPeerConnection !== 'undefined';
    const callLine = onLine ? new VoiceLine() : null;
    line = callLine;
    if (debug) callTrace.start({ line: onLine, tts, languages, turnThreshold, waiting, holdVolume });
    if (debug && callLine) callTrace.watchStats(() => callLine.stats());
    const listener = new MicListener({ line: onLine });
    // Off the line, the voice plays through the microphone's own context (opened in this tap,
    // proven running), never a second one opened later while the phone is in call mode.
    speaker = callLine
      ? new LineSpeaker(callLine, { contextOf: () => listener.audioContext, cache: !incognito })
      : tts === 'server'
        ? new ServerSpeaker({ contextOf: () => listener.audioContext, cache: !incognito })
        : new DeviceSpeaker();
    const toneCues = new ToneCues({
      contextOf: () => listener.audioContext,
      suppress: (ms) => listener.suppress(ms),
      waiting,
      volume: holdVolume,
      onHold: (on) => listener.setHold(on),
      // On the speaker the page's own music reached the microphone; on the line it is the call's.
      musicAllowed: (): boolean => callLine !== null || call.state.route !== 'speaker',
    });
    const call: CallSession = new CallSession({
      listener,
      speaker,
      transcriber: {
        // The call's language goes along as a hint: a few words are too few for Whisper to
        // guess from, and a wrong guess came back as Greek letters (2026-09-16).
        transcribe: async (audio, language, o) => {
          const t0 = performance.now();
          callTrace.mark('stt.send', { bytes: audio.size, type: audio.type, language, turn: o?.turn ?? false, offsetMs: o?.offsetMs ?? 0 });
          try {
            const res = await api.stt(audio, language, o);
            callTrace.mark('stt', { ms: Math.round(performance.now() - t0), text: res.text, language: res.language, turnComplete: res.turn_complete ?? null });
            return { text: res.text, language: res.language, turnComplete: res.turn_complete ?? null };
          } catch (e) {
            callTrace.mark('stt.error', { ms: Math.round(performance.now() - t0), error: e instanceof Error ? e.message : String(e) });
            throw e;
          }
        },
      },
      transport: {
        create: (text, conversationId) => {
          const clientRef = `v${Date.now().toString(36)}_${(clientRefSeq++).toString(36)}`;
          if (!conversationId) {
            // Same path as a typed first message: the server's new conversation opens behind the call.
            set({ pendingNewConversation: { clientRef, text }, messages: { ...get().messages, [NEW_CONVERSATION_KEY]: [] } });
          }
          return socket.send({
            type: 'run.create',
            conversation_id: conversationId,
            text,
            kind: 'chat',
            client_ref: clientRef,
            think: null,
            think_level: null,
            channel: 'voice',
            ...deviceTimezone(),
            ...(callLife ? { call_id: callLife.id } : {}),
            ...callMeField(takeCallMe(set, get, conversationId)),
            // Same as a typed first message: the draft's privacy opens the chat (a call started
            // with the eye on was opening an ordinary, remembered chat - 2026-09-13).
            ...(conversationId ? {} : { incognito: s.draftPrivacy.incognito, ttl_seconds: s.draftPrivacy.ttlSeconds }),
          });
        },
        steer: (runId, text) => socket.send({ type: 'run.steer', run_id: runId, text, channel: 'voice', ...(callLife ? { call_id: callLife.id } : {}) }),
        cancel: (runId) => void socket.send({ type: 'run.cancel', run_id: runId }),
      },
      language: languages[0] ?? 'bg',
      languages,
      turnThreshold,
      conversationId: open,
      // A call on the line opens where the phone puts a call (the earpiece); the toggle moves it.
      route: callLine ? 'earpiece' : (opts?.route ?? readRoute()),
      // The call's quiet signals, on the microphone's own context so they are on its route and
      // hushing the microphone for as long as they sound; on the line the hold music is the call's.
      cues: callLine ? new LineCues(callLine, toneCues, { waiting, volume: holdVolume, onHold: (on) => listener.setHold(on) }) : toneCues,
      // Only the live call draws the screen: a hung-up session finishing a recognition must not
      // bring the call screen back.
      onChange: (state) => {
        if (session !== call) return;
        const was = get().call;
        if (was?.phase !== state.phase || was.muted !== state.muted || was.route !== state.route || was.online !== state.online) {
          callTrace.mark('phase', { phase: state.phase, muted: state.muted, route: state.route, online: state.online, problem: state.problem });
        }
        callTrace.sample('level', 100, { v: Math.round(state.level * 1000) / 1000 });
        set({ call: { ...state } });
      },
    });
    session = call;
    callStarting = false;
    callLife = new CallLife({ send: (m) => socket.send(m), beacon: (id) => api.calls.end(id) }, open);
    callLife.start();
    set({ callId: callLife.id });
    set({ callLine: callLine !== null });
    if (callLine) {
      callLine.onDown = () => {
        listener.lineDown();
        if (line === callLine) set({ callLine: false });
      };
    }
    if (open) socket.send({ type: 'subscribe', conversation_id: open });
    await call.start();
    if (session !== call) return; // hung up while the microphone was being asked for
    // The line only once the microphone (echo cancelling on) is open: Chrome on Android fixes an
    // output's kind when it opens - a call's (earpiece, call volume) only while such a microphone
    // is open, media (the loudspeaker at the bottom) otherwise (listener.ts). Connected beside the
    // microphone, the line's sound could open first: at the ear the cheek covered the speaker and
    // Jarvis "disappeared" (2026-10-16).
    if (callLine && call.state.phase !== 'ended') void callLine.connect().catch(() => callLine.giveUp());
    if (opts?.say && call.state.phase !== 'ended') call.say(opts.say);
    if (call.state.phase === 'ended') {
      // The microphone was refused or is missing: the reason is on the screen, the call is over.
      const problem = call.state.problem;
      get().endCall();
      if (problem) get().notify(problem, 'error');
    }
  },

  endCall: () => {
    // Hang-up first, while the socket is surely there: what was noted on the call starts now.
    callLife?.end();
    callLife = null;
    set({ callId: null });
    session?.end();
    callTrace.stop();
    session = null;
    if (speaker instanceof ServerSpeaker || speaker instanceof LineSpeaker) speaker.close();
    speaker = null;
    line?.close();
    line = null;
    releaseScreen();
    set({ call: null });
  },

  receiveRing: (ring) => {
    if (session) return; // already on a call with him
    set({ incoming: ring });
    const id = ring.conversationId;
    if (id === null) return;
    if (id !== get().openConversationId) void get().openConversation(id, { replace: true });
    // A ring for a chat that is gone (deleted, expired) has nothing to answer into.
    api.conversations.get(id).catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 404 && get().incoming === ring) set({ incoming: null });
    });
    if (ring.answered) void answerFromNotification(get, ring);
  },

  answerRing: async () => {
    const ring = get().incoming;
    if (!ring) return;
    set({ incoming: null });
    // A ring that came by a link without the words (Discord's): the run's own reply, from the chat.
    let say = ring.say;
    if (!say && ring.conversationId) {
      const reply = (get().messages[ring.conversationId] ?? []).filter((m) => m.run_id === ring.runId && m.role === 'assistant' && m.content.trim()).at(-1);
      say = reply ? firstSentences(reply.content, 3) : '';
    }
    await get().startCall({ say });
  },

  declineRing: () => set({ incoming: null }),

  toggleCallMe: (opts) => {
    const s = get();
    const key = s.openConversationId ?? NEW_CONVERSATION_KEY;
    const run = selectActiveRun(s, s.openConversationId);
    const on = run ? !run.call_me : !(s.callMeNext[key] ?? (opts?.voice ? false : s.callMeDefault));
    if (run) {
      if (!ws?.send({ type: 'run.call_me', run_id: run.id, on })) {
        s.notify('Not connected to Jarvis.', 'error');
        return;
      }
    } else {
      set((st) => ({ callMeNext: { ...st.callMeNext, [key]: on } }));
    }
    if (!on || !pushSupported()) return;
    // The first ring on this device needs its permission, asked inside this tap.
    if (Notification.permission === 'denied') {
      s.notify('Notifications are blocked for Jarvis here, so this device will not ring - your other devices or Discord will.');
    } else if (Notification.permission === 'default') {
      void enablePush()
        .then((status) => {
          if (status !== 'on') s.notify('This device will not ring - your other devices or Discord will.');
        })
        .catch((e: unknown) => s.notify(`This device cannot ring: ${errorText(e)}`, 'error'));
    }
  },

  setCallMeDefault: (on) => set({ callMeDefault: on }),

  setCallMuted: (muted) => session?.setMuted(muted),

  setCallRoute: (route) => {
    if (line && !line.down && session) {
      // On the line the route is the call's output, chosen without touching the microphone or
      // what is being said. A browser that offers no choice gets the old way: the call starts
      // again with the page playing the voice, on the speaker.
      const call = session;
      void line.nextOutput().then((moved) => {
        if (session !== call) return;
        if (moved) call.noteRoute(route);
        else {
          get().endCall();
          void get().startCall({ page: true, route });
        }
      });
      return;
    }
    writeRoute(route);
    void session?.setRoute(route);
  },

  openConversation: async (id, opts = {}) => {
    const prev = get().openConversationId;
    if (prev && prev !== id) ws?.send({ type: 'unsubscribe', conversation_id: prev });
    set({ openConversationId: id, view: opts.stay ? get().view : 'chat', sidebarOpen: false, pendingNewConversation: null, freshChat: false });
    rememberConversation(id);
    if (!opts.silent && !opts.stay) navigate('chat', id, opts.replace ?? false);
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
      draftPrivacy: DRAFT_NORMAL,
      freshChat: true,
      messages: omit(s.messages, NEW_CONVERSATION_KEY),
    }));
    rememberConversation(null);
    navigate('chat', null);
  },

  refreshConversation: async (id) => {
    set({ loadingMessagesFor: id });
    try {
      const [conv, serverMessages, serverRuns, summary, tasks] = await Promise.all([
        get().conversations[id] ? Promise.resolve(null) : api.conversations.get(id),
        api.conversations.messages(id),
        api.conversations.runs(id),
        api.conversations.summary(id).catch(() => null),
        // The cards of this chat. A core without tasks (older build) answers 404: no cards.
        api.tasks.list(id).catch(() => [] as Task[]),
      ]);
      set((s) => ({ ...mergeConversationData(s, id, conv, serverMessages, serverRuns), summaries: { ...s.summaries, [id]: summary } }));
      if (tasks.length) set((s) => tasks.reduce((acc, t) => taskChanged(acc, t), s));
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
      hasAttachments: attachments.length > 0,
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
      channel: 'text',
      created_at: new Date().toISOString(),
      optimistic: true,
    };
    const key = open ?? NEW_CONVERSATION_KEY;
    set((st) => ({
      messages: { ...st.messages, [key]: [...(open ? (st.messages[key] ?? []) : []), optimistic] },
      pendingNewConversation: open ? st.pendingNewConversation : { clientRef, text: trimmed },
      pendingAttachments: [],
    }));
    // A run already working gets the message handed to it instead of a new turn being started:
    // it reads it at its next step, keeping everything it has done so far. Attachments stay a
    // new-turn thing — the run has already assembled its context.
    const active = selectActiveRun(s, open);
    if (active) {
      return socket.send({ type: 'run.steer', run_id: active.id, text: trimmed, client_ref: clientRef });
    }
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
      ...deviceTimezone(),
      ...callMeField(takeCallMe(set, get, open)),
      // Only a first message opens a chat, so only then does the draft's privacy travel.
      ...(open ? {} : { incognito: s.draftPrivacy.incognito, ttl_seconds: s.draftPrivacy.ttlSeconds }),
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

  cancelTask: (taskId) => {
    if (!ws?.send({ type: 'task.cancel', task_id: taskId })) get().notify('Not connected — could not cancel the task.', 'error');
  },

  steerTask: (taskId, text) => {
    const words = text.trim();
    if (!words) return false;
    if (!ws?.send({ type: 'task.steer', task_id: taskId, text: words })) {
      get().notify('Not connected — the task did not get it.', 'error');
      return false;
    }
    return true;
  },

  allowToolInTasks: (taskId, callId) => {
    if (!ws?.send({ type: 'task.allow_tool', task_id: taskId, call_id: callId })) {
      get().notify('Not connected — could not send the decision.', 'error');
    }
  },

  openTaskWork: (task) => {
    if (task.conversation_id) void get().openConversation(task.conversation_id);
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

  setConversationInstructions: async (id, instructions) => {
    try {
      const conv = await api.conversations.patch(id, { instructions });
      upsertConversation(set, conv);
      get().notify(instructions.trim() ? 'Instructions saved for this chat.' : 'Instructions cleared.');
    } catch (e) {
      get().notify(`Could not save the instructions: ${errorText(e)}`, 'error');
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
        const att = await api.attachments.upload(file, get().openConversationId, selectIncognitoNow(get()));
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
      const att = await api.attachments.uploadText(text, name, get().openConversationId, selectIncognitoNow(get()));
      set((s) => ({ pendingAttachments: [...s.pendingAttachments, att] }));
      return true;
    } catch (e) {
      get().notify(`Could not attach the text: ${errorText(e)}`, 'error');
      return false;
    }
  },

  removeAttachment: (id) => {
    set((s) => ({ pendingAttachments: s.pendingAttachments.filter((a) => a.id !== id) }));
    api.attachments.remove(id).catch(() => undefined); // best effort: the row is orphaned anyway
  },

  addAnnotation: (conversationId, note) =>
    set((s) => ({ annotations: { ...s.annotations, [conversationId]: [...(s.annotations[conversationId] ?? []), note] } })),
  removeAnnotation: (conversationId, id) =>
    set((s) => ({ annotations: { ...s.annotations, [conversationId]: (s.annotations[conversationId] ?? []).filter((a) => a.id !== id) } })),
  clearAnnotations: (conversationId) => set((s) => ({ annotations: omit(s.annotations, conversationId) })),

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

  loadFolders: async () => {
    try {
      set({ folders: await api.folders.list() });
    } catch (e) {
      // Not worth a toast: the sidebar simply shows the flat list until the next attempt.
      if (!(e instanceof ApiError && e.status === 401)) console.warn('folders:', errorText(e));
    }
  },

  loadSessions: async () => {
    try {
      set({ sessions: await api.sessions.list() });
    } catch (e) {
      // The "@" menu simply stays empty; nothing else depends on this.
      if (!(e instanceof ApiError && e.status === 401)) console.warn('sessions:', errorText(e));
    }
  },

  createFolder: async (name) => {
    const trimmed = name.trim();
    if (!trimmed) return null;
    try {
      const folder = await api.folders.create(trimmed);
      set((s) => ({ folders: [...s.folders.filter((f) => f.id !== folder.id), folder] }));
      return folder.id;
    } catch (e) {
      get().notify(`Could not create the folder: ${errorText(e)}`, 'error');
      return null;
    }
  },

  renameFolder: async (id, name) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    try {
      const folder = await api.folders.patch(id, { name: trimmed });
      set((s) => ({ folders: s.folders.map((f) => (f.id === id ? folder : f)) }));
    } catch (e) {
      get().notify(`Rename failed: ${errorText(e)}`, 'error');
    }
  },

  deleteFolder: async (id) => {
    try {
      await api.folders.remove(id);
      // The chats survive; the server re-announces each one with folder_id cleared.
      set((s) => ({ folders: s.folders.filter((f) => f.id !== id) }));
    } catch (e) {
      get().notify(`Could not delete the folder: ${errorText(e)}`, 'error');
    }
  },

  moveConversation: async (id, folderId) => {
    try {
      upsertConversation(set, await api.conversations.patch(id, { folder_id: folderId }));
      void get().loadFolders(); // the counts on the folder heads moved with it
    } catch (e) {
      get().notify(`Could not move the chat: ${errorText(e)}`, 'error');
    }
  },

  toggleFolderCollapsed: (id) => {
    set((s) => {
      const next = s.collapsedFolders.includes(id) ? s.collapsedFolders.filter((f) => f !== id) : [...s.collapsedFolders, id];
      writeCollapsedFolders(next);
      return { collapsedFolders: next };
    });
  },

  setAttentionOnly: (on) => set({ attentionOnly: on }),

  openConversationAt: async (conversationId, messageId) => {
    // Set before opening: the transcript mounts pinned to the bottom and looks for this on its
    // first render with messages, which happens inside openConversation's refresh.
    set({ scrollToMessage: messageId });
    await get().openConversation(conversationId);
  },

  clearScrollToMessage: () => set({ scrollToMessage: null }),

  toggleSelected: (id) => {
    set((s) => ({
      selection: s.selection.includes(id) ? s.selection.filter((x) => x !== id) : [...s.selection, id],
      selectionAnchor: id,
    }));
  },

  extendSelection: (id, ordered) => {
    const s = get();
    const anchor = s.selectionAnchor ?? id;
    const from = ordered.indexOf(anchor);
    const to = ordered.indexOf(id);
    if (from < 0 || to < 0) {
      get().toggleSelected(id);
      return;
    }
    const span = ordered.slice(Math.min(from, to), Math.max(from, to) + 1);
    set({ selection: [...new Set([...s.selection, ...span])], selectionAnchor: id });
  },

  setSelection: (ids) => set({ selection: [...new Set(ids)], selectionAnchor: ids.at(-1) ?? null }),
  clearSelection: () => set({ selection: [], selectionAnchor: null }),

  markAllRead: async () => {
    const ids = selectUnreadIds(get().conversations);
    if (ids.length === 0) return;
    try {
      const result = await api.conversations.bulk(ids, 'read');
      const ts = new Date().toISOString();
      get().applyEvents(result.updated.map((conversation) => ({ type: 'conversation.updated' as const, ts, conversation })));
    } catch (e) {
      get().notify(`Could not mark the chats read: ${errorText(e)}`, 'error');
    }
  },

  bulkSelected: async (action, folderId) => {
    const ids = get().selection;
    if (ids.length === 0) return;
    try {
      const result = await api.conversations.bulk(ids, action, folderId);
      const ts = new Date().toISOString();
      // Through the reducer, not straight into `conversations`: a deleted chat also has
      // messages, runs, streams and possibly the open-conversation pointer to clean up.
      get().applyEvents([
        ...result.deleted.map((id) => ({ type: 'conversation.deleted' as const, ts, conversation_id: id })),
        ...result.updated.map((conversation) => ({ type: 'conversation.updated' as const, ts, conversation })),
      ]);
      set({ selection: [], selectionAnchor: null });
      if (get().openConversationId === null) navigate('chat', null, true);
      void get().loadFolders();
      const n = action === 'delete' ? result.deleted.length : result.updated.length;
      get().notify(`${BULK_DONE[action]} ${n} ${n === 1 ? 'chat' : 'chats'}.`);
    } catch (e) {
      get().notify(`Could not ${action} the selected chats: ${errorText(e)}`, 'error');
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

  setDraftPrivacy: (privacy) => set({ draftPrivacy: privacy }),

  setConversationTtl: async (id, ttlSeconds) => {
    try {
      const conv = await api.conversations.patch(id, { ttl_seconds: ttlSeconds });
      upsertConversation(set, conv);
      get().notify(ttlSeconds === null ? 'This chat is kept.' : `This chat disappears after ${ttlLabel(ttlSeconds)} of quiet.`);
    } catch (e) {
      get().notify(`Could not change when the chat disappears: ${errorText(e)}`, 'error');
    }
  },

  notify: (text, level = 'info') => set({ notice: { id: ++noticeSeq, level, text } }),
  dismissNotice: () => set({ notice: null }),
}));

// ---- helpers -----------------------------------------------------------------------------

type Set = (partial: Partial<AppState> | ((s: AppState) => Partial<AppState>)) => void;
type Get = () => AppState;

/**
 * Answer was tapped on the notification: that was the answer, so the call opens without the
 * incoming screen asking a second time. Only when this page may start sound without a tap of its
 * own (an installed app, or a site allowed to autoplay) - otherwise the incoming screen stays,
 * and its button is the tap the microphone and the voice need.
 */
async function answerFromNotification(get: Get, ring: IncomingRing): Promise<void> {
  if (!maySoundWithoutTap()) return;
  // Opened cold by the notification: the socket is still connecting.
  for (let waited = 0; !ws?.isOpen && waited < 8000; waited += 100) await new Promise((r) => setTimeout(r, 100));
  if (!ws?.isOpen || get().incoming !== ring) return; // still offline, or declined / gone meanwhile
  await get().answerRing();
}

function maySoundWithoutTap(): boolean {
  const policy = (navigator as Navigator & { getAutoplayPolicy?: (kind: 'audiocontext') => string }).getAutoplayPolicy;
  if (policy) return policy.call(navigator, 'audiocontext') === 'allowed';
  if (typeof AudioContext === 'undefined') return false;
  try {
    // A context is born running only where sound may start without a gesture.
    const probe = new AudioContext();
    const running = probe.state === 'running';
    void probe.close();
    return running;
  } catch {
    return false;
  }
}

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

/** Every open task, for the header chip of whichever chat is open (the cards load with their chat). */
async function loadOpenTasks(set: Set): Promise<void> {
  try {
    const open = await api.tasks.list(null, true);
    set((s) => open.reduce((acc, t) => taskChanged(acc, t), s));
  } catch {
    /* an older core has no tasks; nothing to show */
  }
}

function indexBy(list: Conversation[], into: Record<string, Conversation>): Record<string, Conversation> {
  const out = { ...into };
  for (const c of list) out[c.id] = c;
  return out;
}

/** Tell the core whether he can see the app (a ring by default waits until he cannot). */
function sendPresence(): void {
  ws?.send({ type: 'presence', visible: document.visibilityState === 'visible' });
}

/** The bell for the next message in this chat if he touched it, used up by that message. */
function takeCallMe(set: Set, get: Get, conversationId: string | null): boolean | undefined {
  const key = conversationId ?? NEW_CONVERSATION_KEY;
  const on = get().callMeNext[key];
  if (on !== undefined) set((s) => ({ callMeNext: omit(s.callMeNext, key) }));
  return on;
}

/** Untouched, the field is left out and the core's settings decide. */
function callMeField(on: boolean | undefined): { call_me?: boolean } {
  return on === undefined ? {} : { call_me: on };
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
