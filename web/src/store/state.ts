import type { Conversation, Message, Run, RunScopedEvent } from '../protocol/types';

/** A persisted message, or one the UI added optimistically while the server confirms it. */
export type LocalMessage = Message & { optimistic?: boolean };

/** Live streaming buffer for one run (never persisted; rebuilt from deltas). */
export interface StreamState {
  runId: string;
  conversationId: string;
  text: string;
  reasoning: string;
  /** Client clock (ms) when the run was first seen, for the placeholder timer. */
  startedAt: number;
  reasoningStartedAt: number | null;
  reasoningEndedAt: number | null;
  /** True between model.call and model.done. */
  modelActive: boolean;
  /** Last run-scoped event type, drives the "working on…" activity line. */
  lastEventType: RunScopedEvent['type'] | null;
  /** Last tool name called (for the activity line while a tool runs). */
  lastToolName: string | null;
}

/** The protocol-driven part of the store: everything `applyServerEvent` may touch. */
export interface ChatState {
  conversations: Record<string, Conversation>;
  /** Messages per conversation, oldest first. `undefined` = not loaded yet. */
  messages: Record<string, LocalMessage[] | undefined>;
  runs: Record<string, Run>;
  /** Run ids per conversation, newest first. */
  runsByConversation: Record<string, string[] | undefined>;
  /** Run-scoped events (deltas excluded) per run, in arrival/seq order. */
  runEvents: Record<string, RunScopedEvent[] | undefined>;
  streams: Record<string, StreamState | undefined>;
  openConversationId: string | null;
  /**
   * Set while a message was sent with `conversation_id: null`; the next unknown
   * `conversation.updated` is the conversation the server created for it.
   */
  pendingNewConversation: { clientRef: string; text: string } | null;
  /** Run ids the user asked to cancel; cleared by the terminal event. */
  cancelRequested: Record<string, true | undefined>;
}

/** Key under which optimistic messages live before the server names the conversation. */
export const NEW_CONVERSATION_KEY = '__new__';

/** Immutable "delete": a copy of `record` without `keys` (same object if nothing to drop). */
export function omit<T>(record: Record<string, T>, ...keys: string[]): Record<string, T> {
  if (!keys.some((k) => k in record)) return record;
  const drop = new Set(keys);
  const out: Record<string, T> = {};
  for (const k of Object.keys(record)) if (!drop.has(k)) out[k] = record[k];
  return out;
}

export function initialChatState(): ChatState {
  return {
    conversations: {},
    messages: {},
    runs: {},
    runsByConversation: {},
    runEvents: {},
    streams: {},
    openConversationId: null,
    pendingNewConversation: null,
    cancelRequested: {},
  };
}
