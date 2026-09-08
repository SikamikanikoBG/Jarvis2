import type { ChatFolder, Conversation, ConversationKind, Run, ToolConfirmRequested } from '../protocol/types';
import { isTerminal } from '../protocol/types';
import type { ChatState } from './state';

export type FolderKind = Exclude<ConversationKind, 'chat'>;

export const FOLDER_ORDER: readonly FolderKind[] = ['scheduled', 'triage', 'meeting', 'collab', 'archive'];

export const FOLDER_LABELS: Record<FolderKind, string> = {
  scheduled: 'Scheduled',
  triage: 'Triage',
  meeting: 'Meetings',
  collab: 'Collab',
  archive: 'Archive',
};

export interface FolderGroup {
  key: string;
  label: string;
  conversations: Conversation[];
  unread: number;
}

export interface Folder {
  kind: FolderKind;
  label: string;
  groups: FolderGroup[];
  count: number;
  unread: number;
}

/** One of Arsen's own folders, with the chats filed in it. */
export interface ChatFolderSection {
  folder: ChatFolder;
  conversations: Conversation[];
  unread: number;
  /** Live chats inside, so a collapsed folder can still say something is moving in there. */
  running: number;
  waiting: number;
}

export interface SidebarModel {
  /** Chats in none of Arsen's folders: flat, pinned first, then newest first. */
  chats: Conversation[];
  /** Arsen's own folders, in his order — including the empty ones, which are drop targets. */
  chatFolders: ChatFolderSection[];
  /** The machine's folders (Scheduled, Triage, Meetings, Collab, Archive). */
  folders: Folder[];
}

const byUpdatedDesc = (a: Conversation, b: Conversation) => b.updated_at.localeCompare(a.updated_at);
/** Pinned chats first (their own recency order), then the rest newest first. */
const byPinnedThenUpdated = (a: Conversation, b: Conversation) => Number(b.pinned) - Number(a.pinned) || byUpdatedDesc(a, b);

function folderOf(c: Conversation): FolderKind | 'chat' {
  if (c.archived || c.kind === 'archive') return 'archive';
  return c.kind;
}

/**
 * Chats flat and newest first; everything else grouped by kind, then by folder.
 *
 * A chat Arsen filed in one of his own folders leaves the flat list for that folder. Archiving
 * still wins over filing: an archived chat belongs to the Archive folder no matter where it was
 * filed, so unarchiving it puts it straight back where he had it.
 */
export function selectSidebar(conversations: Record<string, Conversation>, chatFolders: ChatFolder[] = []): SidebarModel {
  const all = Object.values(conversations).sort(byUpdatedDesc);
  const chats: Conversation[] = [];
  const filed = new Map<string, Conversation[]>(chatFolders.map((f) => [f.id, []]));
  const buckets = new Map<FolderKind, Map<string, FolderGroup>>();
  for (const c of all) {
    const f = folderOf(c);
    if (f === 'chat') {
      // An id we do not know (the folder was deleted elsewhere) falls back to the flat list.
      const bucket = c.folder_id ? filed.get(c.folder_id) : undefined;
      (bucket ?? chats).push(c);
      continue;
    }
    const groups = buckets.get(f) ?? new Map<string, FolderGroup>();
    buckets.set(f, groups);
    const key = c.folder_key ?? '';
    const g = groups.get(key) ?? { key, label: c.folder_label ?? c.folder_key ?? '', conversations: [], unread: 0 };
    g.conversations.push(c);
    if (c.unread) g.unread += 1;
    groups.set(key, g);
  }
  chats.sort(byPinnedThenUpdated);
  const sections: ChatFolderSection[] = chatFolders.map((folder) => {
    const list = (filed.get(folder.id) ?? []).sort(byPinnedThenUpdated);
    return {
      folder,
      conversations: list,
      unread: list.filter((c) => c.unread).length,
      running: list.filter((c) => c.activity === 'running').length,
      waiting: list.filter((c) => c.activity === 'waiting').length,
    };
  });
  const folders: Folder[] = [];
  for (const kind of FOLDER_ORDER) {
    const groups = buckets.get(kind);
    if (!groups) continue;
    const list = [...groups.values()]; // insertion order = newest first already
    folders.push({
      kind,
      label: FOLDER_LABELS[kind],
      groups: list,
      count: list.reduce((n, g) => n + g.conversations.length, 0),
      unread: list.reduce((n, g) => n + g.unread, 0),
    });
  }
  return { chats, chatFolders: sections, folders };
}

/** Newest non-terminal run of a conversation, if any. */
export function selectActiveRun(state: ChatState, conversationId: string | null): Run | null {
  if (!conversationId) return null;
  for (const id of state.runsByConversation[conversationId] ?? []) {
    const run = state.runs[id];
    if (run && !isTerminal(run.status)) return run;
  }
  // A stream may exist for a run the runs list does not know yet (run.queued before run.updated).
  for (const s of Object.values(state.streams)) {
    if (s?.conversationId === conversationId) {
      const run = state.runs[s.runId];
      if (run && !isTerminal(run.status)) return run;
    }
  }
  return null;
}

/** The unresolved confirmation request of a run, if the run is waiting on one. */
export function selectPendingConfirm(state: ChatState, runId: string): ToolConfirmRequested | null {
  const run = state.runs[runId];
  if (run && isTerminal(run.status)) return null;
  const events = state.runEvents[runId] ?? [];
  const resolved = new Set<string>();
  let pending: ToolConfirmRequested | null = null;
  for (const ev of events) {
    if (ev.type === 'tool.confirm_resolved') resolved.add(ev.call_id);
    if (ev.type === 'tool.confirm_requested') pending = ev;
  }
  if (pending === null) return null;
  return resolved.has(pending.call_id) ? null : pending;
}

export function selectUnreadCount(conversations: Record<string, Conversation>): number {
  return Object.values(conversations).reduce((n, c) => n + (c.unread ? 1 : 0), 0);
}

/**
 * Why this message cannot go out right now, or null when it can.
 *
 * One answer for the two callers that used to disagree: the composer only greyed the Send
 * button out when the socket was down, while Enter went straight to `store.send`, which
 * refused a second message during a run without a word. Either way the composer cleared the
 * box first, so what Arsen had typed was gone.
 */
export function selectSendRefusal(
  state: ChatState,
  {
    connection,
    hasText,
    hasAttachments = false,
    conversationId,
  }: { connection: string; hasText: boolean; hasAttachments?: boolean; conversationId: string | null },
): string | null {
  if (!hasText) return 'nothing to send';
  if (connection !== 'open') return 'Not connected — the message was not sent.';
  // A run already working is NOT a refusal any more: the message is handed to it and read at
  // its next step. Watching it head the wrong way and having to wait was the worst moment in
  // the loop. Words only, though — a run has already assembled its context, and a picture
  // needs to be part of that from the start.
  if (hasAttachments && selectActiveRun(state, conversationId)) {
    return 'Jarvis is working — words reach him now, but a photo has to wait for the next message.';
  }
  return null;
}
