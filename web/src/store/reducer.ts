/**
 * Pure reducer: one `ServerEvent` in, a new `ChatState` out. No I/O, no clocks other than
 * the `now` argument, so every branch is unit-testable.
 */
import type {
  Conversation,
  Message,
  ModelDelta,
  Run,
  RunKind,
  RunScopedEvent,
  RunStatus,
  ServerEvent,
} from '../protocol/types';
import { isTerminal } from '../protocol/types';
import { NEW_CONVERSATION_KEY, omit, type ChatState, type LocalMessage, type StreamState } from './state';

export function applyServerEvent<S extends ChatState>(state: S, event: ServerEvent, now: number): S {
  switch (event.type) {
    case 'pong':
      return state;
    case 'conversation.updated':
      return conversationUpdated(state, event.conversation);
    case 'conversation.deleted':
      return conversationDeleted(state, event.conversation_id);
    case 'message.created':
      return messageCreated(state, event.message);
    case 'run.updated':
      return runUpdated(state, event.run, now);
    case 'model.delta':
      return modelDelta(state, event, now);
    case 'board.changed':
    case 'kg.changed':
    case 'skills.changed':
    case 'schedule.changed':
    case 'tools.changed':
    case 'meeting.segment':
    case 'meeting.changed':
      return state; // feature screens react to these in the store (refetch), not in chat state
    default:
      return runScoped(state, event, now);
  }
}

export function applyServerEvents<S extends ChatState>(state: S, events: ServerEvent[], now: number): S {
  return events.reduce((acc, ev) => applyServerEvent(acc, ev, now), state);
}

// ---- conversations ---------------------------------------------------------------------

function conversationUpdated<S extends ChatState>(state: S, conv: Conversation): S {
  const known = conv.id in state.conversations;
  // The conversation on screen is read by definition; the store PATCHes the server to match.
  const shown = conv.id === state.openConversationId && conv.unread ? { ...conv, unread: false } : conv;
  let next: S = { ...state, conversations: { ...state.conversations, [conv.id]: shown } };
  if (!known && state.pendingNewConversation) {
    // The server created this conversation for our `run.create {conversation_id: null}`.
    const optimistic = state.messages[NEW_CONVERSATION_KEY] ?? [];
    const messages = omit(next.messages, NEW_CONVERSATION_KEY);
    next = {
      ...next,
      messages: { ...messages, [conv.id]: optimistic.map((m) => ({ ...m, conversation_id: conv.id })) },
      openConversationId: conv.id,
      pendingNewConversation: null,
    };
  }
  return next;
}

function conversationDeleted<S extends ChatState>(state: S, id: string): S {
  const runIds = state.runsByConversation[id] ?? [];
  return {
    ...state,
    conversations: omit(state.conversations, id),
    messages: omit(state.messages, id),
    runs: omit(state.runs, ...runIds),
    runEvents: omit(state.runEvents, ...runIds),
    streams: omit(state.streams, ...runIds),
    runsByConversation: omit(state.runsByConversation, id),
    openConversationId: state.openConversationId === id ? null : state.openConversationId,
  };
}

// ---- messages --------------------------------------------------------------------------

function messageCreated<S extends ChatState>(state: S, message: Message): S {
  const cid = message.conversation_id;
  if (!cid) return state;
  const list = state.messages[cid] ?? [];
  let replaced = false;
  const merged: LocalMessage[] = list.map((m) => {
    if (replaced) return m;
    const sameId = message.id !== null && m.id === message.id;
    const sameOptimistic = m.optimistic === true && m.role === message.role && m.content === message.content;
    if (sameId || sameOptimistic) {
      replaced = true;
      return message;
    }
    return m;
  });
  if (!replaced) merged.push(message);

  let streams = state.streams;
  const s = message.role === 'assistant' && message.run_id ? state.streams[message.run_id] : undefined;
  if (s) {
    // The persisted message replaces the streaming buffer for this model call.
    streams = { ...streams, [s.runId]: { ...s, text: '', reasoning: '', reasoningStartedAt: null, reasoningEndedAt: null } };
  }
  return { ...state, messages: { ...state.messages, [cid]: merged }, streams };
}

// ---- runs ------------------------------------------------------------------------------

function addRunToConversation(byConv: ChatState['runsByConversation'], run: Run): ChatState['runsByConversation'] {
  const list = byConv[run.conversation_id] ?? [];
  if (list.includes(run.id)) return byConv;
  return { ...byConv, [run.conversation_id]: [run.id, ...list] };
}

function newStream(runId: string, conversationId: string, now: number): StreamState {
  return {
    runId,
    conversationId,
    text: '',
    reasoning: '',
    startedAt: now,
    reasoningStartedAt: null,
    reasoningEndedAt: null,
    modelActive: false,
    lastEventType: null,
    lastToolName: null,
  };
}

function runUpdated<S extends ChatState>(state: S, run: Run, now: number): S {
  const runs = { ...state.runs, [run.id]: run };
  const runsByConversation = addRunToConversation(state.runsByConversation, run);
  let streams = state.streams;
  let cancelRequested = state.cancelRequested;
  if (isTerminal(run.status)) {
    streams = omit(streams, run.id);
    cancelRequested = omit(cancelRequested, run.id);
  } else if (!streams[run.id]) {
    // Re-subscribe after reconnect: the server replays non-terminal runs this way.
    streams = { ...streams, [run.id]: { ...newStream(run.id, run.conversation_id, now), lastEventType: 'run.started' } };
  }
  return { ...state, runs, runsByConversation, streams, cancelRequested };
}

function stubRun(ev: RunScopedEvent, kind: RunKind, inputText: string): Run {
  return {
    id: ev.run_id,
    conversation_id: ev.conversation_id,
    kind,
    status: 'queued',
    input_text: inputText,
    plan: null,
    budget: { max_steps: 0, max_tokens: 0, max_seconds: 0 },
    priority: 0,
    steps_used: 0,
    usage: { prompt_tokens: 0, completion_tokens: 0, calls: 0, ttft_ms: null, duration_ms: 0 },
    last_seq: 0,
    error: null,
    waiting_reason: null,
    think: null,
    think_level: null,
    created_at: ev.ts,
    started_at: null,
    finished_at: null,
  };
}

function patchRun(runs: Record<string, Run>, id: string, patch: Partial<Run>): Record<string, Run> {
  const cur = runs[id];
  if (!cur) return runs;
  return { ...runs, [id]: { ...cur, ...patch } };
}

function appendEvent(events: ChatState['runEvents'], ev: RunScopedEvent): ChatState['runEvents'] {
  const list = events[ev.run_id] ?? [];
  // Persisted events have seq > 0; drop exact replays (reconnect + REST heal overlap).
  if (ev.seq > 0 && list.some((e) => e.seq === ev.seq)) return events;
  return { ...events, [ev.run_id]: [...list, ev] };
}

function modelDelta<S extends ChatState>(state: S, ev: ModelDelta, now: number): S {
  const s = state.streams[ev.run_id] ?? newStream(ev.run_id, ev.conversation_id, now);
  const next: StreamState =
    ev.kind === 'reasoning'
      ? {
          ...s,
          reasoning: s.reasoning + ev.text,
          reasoningStartedAt: s.reasoningStartedAt ?? now,
          modelActive: true,
          lastEventType: 'model.delta',
        }
      : {
          ...s,
          text: s.text + ev.text,
          reasoningEndedAt: s.reasoningStartedAt !== null && s.reasoningEndedAt === null ? now : s.reasoningEndedAt,
          modelActive: true,
          lastEventType: 'model.delta',
        };
  return { ...state, streams: { ...state.streams, [ev.run_id]: next } };
}

/** Bind our optimistic user message to the run (and its server id) so the transcript groups it. */
function bindOptimistic(messages: ChatState['messages'], conversationId: string, runId: string, userMessageId: string | null): ChatState['messages'] {
  const list = messages[conversationId];
  if (!list) return messages;
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    if (m && m.optimistic && m.role === 'user' && m.run_id === null) {
      const bound: LocalMessage = { ...m, run_id: runId, id: userMessageId ?? m.id };
      return { ...messages, [conversationId]: list.map((x, j) => (j === i ? bound : x)) };
    }
  }
  return messages;
}

function partialFromStream(s: StreamState, ev: Extract<RunScopedEvent, { type: 'run.cancelled' }>): LocalMessage {
  return {
    id: ev.partial_message_id,
    conversation_id: ev.conversation_id,
    run_id: ev.run_id,
    role: 'assistant',
    content: s.text,
    reasoning: s.reasoning || null,
    tool_calls: [],
    tool_call_id: null,
    name: null,
    partial: true,
    created_at: ev.ts,
  };
}

function runScoped<S extends ChatState>(state: S, ev: Exclude<RunScopedEvent, ModelDelta>, now: number): S {
  let runs = state.runs;
  let runsByConversation = state.runsByConversation;
  let streams = state.streams;
  let conversations = state.conversations;
  let messages = state.messages;
  let cancelRequested = state.cancelRequested;
  const runEvents = appendEvent(state.runEvents, ev);

  const stream = (): StreamState => streams[ev.run_id] ?? newStream(ev.run_id, ev.conversation_id, now);
  const setStream = (patch: Partial<StreamState>) => {
    streams = { ...streams, [ev.run_id]: { ...stream(), ...patch, lastEventType: ev.type } };
  };
  const setStatus = (status: RunStatus, extra: Partial<Run> = {}) => {
    runs = patchRun(runs, ev.run_id, { status, ...extra });
  };
  const endStream = () => {
    streams = omit(streams, ev.run_id);
    cancelRequested = omit(cancelRequested, ev.run_id);
  };

  switch (ev.type) {
    case 'run.queued': {
      if (!runs[ev.run_id]) {
        const run = stubRun(ev, ev.kind, ev.input_preview);
        runs = { ...runs, [run.id]: run };
        runsByConversation = addRunToConversation(runsByConversation, run);
      }
      messages = bindOptimistic(messages, ev.conversation_id, ev.run_id, ev.user_message_id);
      setStream({});
      break;
    }
    case 'run.started':
      setStatus('running', { started_at: ev.ts });
      setStream({});
      break;
    case 'run.resumed':
      setStatus('running');
      setStream({});
      break;
    case 'run.interrupted':
      setStatus('interrupted');
      setStream({});
      break;
    case 'run.waiting_user':
      setStatus('waiting_user', { waiting_reason: ev.reason });
      setStream({ modelActive: false });
      break;
    case 'run.done': {
      setStatus('done', { finished_at: ev.ts, usage: ev.usage, steps_used: ev.steps_used });
      endStream();
      const conv = ev.conversation_id !== state.openConversationId ? conversations[ev.conversation_id] : undefined;
      if (conv && !conv.unread) conversations = { ...conversations, [conv.id]: { ...conv, unread: true } };
      break;
    }
    case 'run.failed':
      setStatus('failed', { finished_at: ev.ts, error: ev.error });
      endStream();
      break;
    case 'run.cancelled': {
      setStatus('cancelled', { finished_at: ev.ts });
      const s = streams[ev.run_id];
      if (s && (s.text || s.reasoning) && ev.partial_message_id) {
        // Keep the partial text visible until (or in case) the server's message.created arrives.
        const list = messages[ev.conversation_id] ?? [];
        if (!list.some((m) => m.id === ev.partial_message_id)) {
          messages = { ...messages, [ev.conversation_id]: [...list, partialFromStream(s, ev)] };
        }
      }
      endStream();
      break;
    }
    case 'model.call':
      setStatus('running');
      setStream({ modelActive: true, text: '', reasoning: '', reasoningStartedAt: null, reasoningEndedAt: null });
      break;
    case 'model.done': {
      const s = stream();
      setStream({
        modelActive: false,
        reasoningEndedAt: s.reasoningStartedAt !== null && s.reasoningEndedAt === null ? now : s.reasoningEndedAt,
      });
      break;
    }
    case 'tool.call':
      setStream({ lastToolName: ev.name });
      break;
    case 'tool.confirm_requested':
      setStream({ lastToolName: ev.name, modelActive: false });
      break;
    case 'plan.created':
      runs = patchRun(runs, ev.run_id, { plan: ev.plan });
      setStream({});
      break;
    case 'plan.step_started':
    case 'plan.step_done': {
      const plan = runs[ev.run_id]?.plan;
      if (plan) {
        const steps = plan.steps.map((s, i) => {
          if (ev.type === 'plan.step_started') {
            if (i === ev.index) return { ...s, status: 'in_progress' as const };
            return s.status === 'in_progress' ? { ...s, status: 'done' as const } : s;
          }
          return i === ev.index ? { ...s, status: 'done' as const } : s;
        });
        runs = patchRun(runs, ev.run_id, { plan: { ...plan, steps } });
      }
      setStream({});
      break;
    }
    case 'tool.result':
    case 'tool.confirm_resolved':
    case 'guard.armed':
    case 'guard.consumed':
    case 'judge.verdict':
    case 'context.skills':
      setStream({});
      break;
  }

  return { ...state, runs, runsByConversation, streams, conversations, messages, runEvents, cancelRequested };
}
