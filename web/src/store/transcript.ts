/**
 * Turns messages + run events into the ordered list the Chat transcript renders.
 *
 * One block per run: [user message] → merged-by-timestamp assistant messages, tool cards and
 * system notes → live stream (if any) → run chip. Tool cards are keyed by call_id and merge
 * every source that knows about the call: the assistant message's `tool_calls`, the tool-role
 * message, and the `tool.call` / `tool.result` / `tool.confirm_*` events when they are loaded.
 */
import type { ConversationSummary, Plan, Run, RunScopedEvent, ToolResult } from '../protocol/types';
import { isTerminal } from '../protocol/types';
import type { ChatState, LocalMessage } from './state';

export interface ToolCardModel {
  runId: string;
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  readOnly: boolean | null;
  result: ToolResult | null;
  /** Text of the persisted tool message when no typed result is known. */
  resultText: string | null;
  durationMs: number | null;
  confirm: { reason: string; approved: boolean | null; note: string | null } | null;
  /** True while the call has neither a result nor a tool message. */
  pending: boolean;
}

export type NoteLevel = 'info' | 'warn' | 'error';

export interface NoteModel {
  runId: string;
  level: NoteLevel;
  tag: string;
  title: string;
  detail: string | null;
  plan?: Plan;
}

export type TranscriptItem =
  | { kind: 'message'; key: string; message: LocalMessage }
  | { kind: 'tool'; key: string; card: ToolCardModel }
  | { kind: 'note'; key: string; note: NoteModel }
  | {
      kind: 'confirm';
      key: string;
      runId: string;
      callId: string;
      name: string;
      arguments: Record<string, unknown>;
      reason: string;
    }
  | { kind: 'stream'; key: string; runId: string }
  | { kind: 'run'; key: string; runId: string }
  | { kind: 'summary'; key: string; text: string };

interface Block {
  order: number;
  runId: string | null;
  messages: LocalMessage[];
}

type Timed = { ts: number; msg?: LocalMessage; ev?: RunScopedEvent };

/** The slices the transcript depends on — selected shallowly so deltas do not rebuild it. */
export interface TranscriptSource {
  messages: LocalMessage[];
  /** Run ids of the conversation, newest first. */
  runIds: string[];
  runs: Record<string, Run>;
  runEvents: Record<string, RunScopedEvent[] | undefined>;
  /** Runs that currently have a live stream buffer. */
  streamRunIds: ReadonlySet<string>;
  /** Compaction summary, inserted as a divider after `up_to_message_id`. */
  summary?: ConversationSummary | null;
}

export function transcriptSource(state: ChatState, conversationId: string): TranscriptSource {
  return {
    messages: state.messages[conversationId] ?? [],
    runIds: state.runsByConversation[conversationId] ?? [],
    runs: state.runs,
    runEvents: state.runEvents,
    streamRunIds: new Set(Object.keys(state.streams).filter((k) => state.streams[k])),
    summary: null,
  };
}

export function buildTranscript(state: ChatState, conversationId: string): TranscriptItem[] {
  return buildTranscriptFrom(transcriptSource(state, conversationId));
}

export function buildTranscriptFrom(src: TranscriptSource): TranscriptItem[] {
  const { messages } = src;
  const blocks = new Map<string, Block>();
  messages.forEach((m, idx) => {
    const key = m.run_id ?? `msg:${idx}`;
    const b = blocks.get(key) ?? { order: idx, runId: m.run_id, messages: [] };
    b.messages.push(m);
    blocks.set(key, b);
  });
  const runIds = [...src.runIds].reverse();
  runIds.forEach((rid, i) => {
    if (!blocks.has(rid)) blocks.set(rid, { order: messages.length + i, runId: rid, messages: [] });
  });
  const out: TranscriptItem[] = [];
  for (const b of [...blocks.values()].sort((a, z) => a.order - z.order)) {
    out.push(...buildBlock(src, b));
  }
  if (src.summary) {
    // Everything up to (and including) `up_to_message_id` was compacted: divider right after it,
    // or at the very top when that message is not in the loaded list.
    const item: TranscriptItem = { kind: 'summary', key: `summary:${src.summary.up_to_message_id}`, text: src.summary.text };
    const idx = out.findIndex((i) => i.kind === 'message' && i.message.id === src.summary?.up_to_message_id);
    out.splice(idx + 1, 0, item);
  }
  return out;
}

function buildBlock(src: TranscriptSource, block: Block): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  const runId = block.runId;
  const events = runId ? (src.runEvents[runId] ?? []) : [];
  const run = runId ? src.runs[runId] : undefined;
  const hasStream = runId ? src.streamRunIds.has(runId) : false;
  const cards = collectCards(runId, block.messages, events);
  const emitted = new Set<string>();

  const users = block.messages.filter((m) => m.role === 'user');
  for (const m of users) items.push({ kind: 'message', key: msgKey(m), message: m });

  const timed: Timed[] = [
    ...block.messages.filter((m) => m.role !== 'user').map((m) => ({ ts: Date.parse(m.created_at), msg: m })),
    ...events.map((ev) => ({ ts: Date.parse(ev.ts), ev })),
  ].sort((a, z) => a.ts - z.ts);

  const resolvedConfirms = new Set(
    events.filter((e) => e.type === 'tool.confirm_resolved').map((e) => e.call_id),
  );
  const runActive = run ? !isTerminal(run.status) : hasStream;

  // Keys carry the run id: call ids are only unique within a run.
  const emitCard = (callId: string) => {
    if (emitted.has(callId)) return;
    const card = cards.get(callId);
    if (!card) return;
    emitted.add(callId);
    items.push({ kind: 'tool', key: `tool:${runId ?? ''}:${callId}`, card });
  };

  for (const t of timed) {
    if (t.msg) {
      const m = t.msg;
      if (m.role === 'tool') continue; // merged into its card
      if (m.role === 'system') {
        items.push({
          kind: 'note',
          key: msgKey(m),
          note: { runId: runId ?? '', level: 'info', tag: 'summary', title: 'Earlier turns summarised', detail: m.content },
        });
        continue;
      }
      items.push({ kind: 'message', key: msgKey(m), message: m });
      for (const tc of m.tool_calls) emitCard(tc.id);
      continue;
    }
    const ev = t.ev;
    if (!ev) continue;
    switch (ev.type) {
      case 'tool.call':
        emitCard(ev.call_id);
        break;
      case 'tool.confirm_requested':
        if (!resolvedConfirms.has(ev.call_id) && runActive) {
          items.push({
            kind: 'confirm',
            key: `confirm:${ev.run_id}:${ev.call_id}`,
            runId: ev.run_id,
            callId: ev.call_id,
            name: ev.name,
            arguments: ev.arguments,
            reason: ev.reason,
          });
        } else if (!cards.has(ev.call_id) || !events.some((e) => e.type === 'tool.call' && e.call_id === ev.call_id)) {
          emitCard(ev.call_id);
        }
        break;
      default: {
        const note = noteFor(ev);
        if (note) items.push({ kind: 'note', key: `ev:${ev.run_id}:${ev.seq || ev.ts}:${ev.type}`, note });
      }
    }
  }
  // Cards known only from a tool message whose assistant message is missing (defensive).
  for (const id of cards.keys()) emitCard(id);

  if (runId && (hasStream || (run && !isTerminal(run.status) && run.status !== 'interrupted'))) {
    items.push({ kind: 'stream', key: `stream:${runId}`, runId });
  }
  if (runId && run) {
    if (events.length === 0) {
      // Events not loaded: synthesise the outcome note from the run head.
      const note = noteForRunHead(run.status, run.error, run.waiting_reason, runId);
      if (note) items.push({ kind: 'note', key: `head:${runId}`, note });
    }
    items.push({ kind: 'run', key: `run:${runId}`, runId });
  }
  return items;
}

function msgKey(m: LocalMessage): string {
  return `msg:${m.id ?? `${m.role}:${m.created_at}:${m.content.length}`}`;
}

function collectCards(runId: string | null, messages: LocalMessage[], events: RunScopedEvent[]): Map<string, ToolCardModel> {
  const cards = new Map<string, ToolCardModel>();
  const get = (callId: string, name: string, args: Record<string, unknown>): ToolCardModel => {
    let c = cards.get(callId);
    if (!c) {
      c = {
        runId: runId ?? '',
        callId,
        name,
        arguments: args,
        readOnly: null,
        result: null,
        resultText: null,
        durationMs: null,
        confirm: null,
        pending: true,
      };
      cards.set(callId, c);
    }
    return c;
  };
  for (const m of messages) {
    if (m.role === 'assistant') for (const tc of m.tool_calls) get(tc.id, tc.name, tc.arguments);
  }
  for (const m of messages) {
    if (m.role === 'tool' && m.tool_call_id) {
      const c = get(m.tool_call_id, m.name ?? 'tool', {});
      c.resultText = m.content;
      c.pending = false;
    }
  }
  for (const ev of events) {
    switch (ev.type) {
      case 'tool.call': {
        const c = get(ev.call_id, ev.name, ev.arguments);
        c.arguments = ev.arguments;
        c.readOnly = ev.read_only;
        break;
      }
      case 'tool.result': {
        const c = get(ev.call_id, ev.name, {});
        c.result = ev.result;
        c.durationMs = ev.duration_ms;
        c.pending = false;
        break;
      }
      case 'tool.confirm_requested': {
        const c = get(ev.call_id, ev.name, ev.arguments);
        if (Object.keys(c.arguments).length === 0) c.arguments = ev.arguments;
        c.confirm = { reason: ev.reason, approved: null, note: null };
        break;
      }
      case 'tool.confirm_resolved': {
        const c = cards.get(ev.call_id);
        if (c) {
          c.confirm = { reason: c.confirm?.reason ?? '', approved: ev.approved, note: ev.note };
          if (!ev.approved) c.pending = false;
        }
        break;
      }
      default:
        break;
    }
  }
  return cards;
}

function noteFor(ev: RunScopedEvent): NoteModel | null {
  const base = { runId: ev.run_id };
  switch (ev.type) {
    case 'guard.armed':
      return { ...base, level: 'warn', tag: 'guard', title: `Guard armed: ${ev.guard}`, detail: ev.detail };
    case 'guard.consumed':
      return { ...base, level: 'info', tag: 'guard', title: `Guard consumed: ${ev.guard}`, detail: ev.detail };
    case 'judge.verdict':
      return {
        ...base,
        level: ev.verdict === 'stop' ? 'error' : ev.verdict === 'nudge' ? 'warn' : 'info',
        tag: 'judge',
        title: ev.verdict === 'stop' ? 'Supervisor stopped the run' : `Supervisor: ${ev.verdict}`,
        detail: ev.reason,
      };
    case 'plan.created':
    case 'plan.step_started':
    case 'plan.step_done':
      return null; // the live checklist under the run chip renders the plan
    case 'run.waiting_user':
      return { ...base, level: 'warn', tag: 'waiting', title: 'Waiting for you', detail: ev.reason };
    case 'run.failed':
      return { ...base, level: 'error', tag: 'failed', title: 'Run failed', detail: ev.error };
    case 'run.cancelled':
      return { ...base, level: 'info', tag: 'stopped', title: 'Stopped', detail: null };
    case 'run.interrupted':
      return { ...base, level: 'warn', tag: 'interrupted', title: 'Interrupted by a core restart', detail: null };
    case 'run.resumed':
      return { ...base, level: 'info', tag: 'resumed', title: 'Resumed', detail: null };
    case 'run.done':
      return ev.summary ? { ...base, level: 'warn', tag: 'budget', title: 'Budget reached', detail: ev.summary } : null;
    case 'context.skills':
      return ev.names.length
        ? { ...base, level: 'info', tag: 'skill', title: `Using skill${ev.names.length > 1 ? 's' : ''}: ${ev.names.join(', ')}`, detail: null }
        : null;
    default:
      return null;
  }
}

function noteForRunHead(status: string, error: string | null, waiting: string | null, runId: string): NoteModel | null {
  switch (status) {
    case 'failed':
      return { runId, level: 'error', tag: 'failed', title: 'Run failed', detail: error };
    case 'cancelled':
      return { runId, level: 'info', tag: 'stopped', title: 'Stopped', detail: null };
    case 'waiting_user':
      return { runId, level: 'warn', tag: 'waiting', title: 'Waiting for you', detail: waiting };
    default:
      return null;
  }
}
