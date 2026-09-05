import { describe, expect, it } from 'vitest';
import type { Conversation, Message, ModelUsage, Run, ServerEvent } from '../protocol/types';
import { applyServerEvent, applyServerEvents } from './reducer';
import { selectActiveRun, selectPendingConfirm, selectSidebar } from './selectors';
import { initialChatState, type ChatState, type LocalMessage } from './state';
import { buildTranscript, buildTranscriptFrom, transcriptSource } from './transcript';

const T0 = Date.parse('2026-09-05T10:00:00.000Z');
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString();
const usage = (p = 0, c = 0): ModelUsage => ({ prompt_tokens: p, completion_tokens: c, calls: 1, ttft_ms: 120, duration_ms: 900 });

const RUN = 'run_1';
const CONV = 'conv_a';

function runScoped(ev: { type: string } & Record<string, unknown>, offset = 0): ServerEvent {
  return { ts: iso(offset), run_id: RUN, conversation_id: CONV, seq: 0, ...ev } as unknown as ServerEvent;
}

function conv(id: string, updated: number, extra: Partial<Conversation> = {}): Conversation {
  return {
    id,
    kind: 'chat',
    title: id,
    folder_key: null,
    folder_label: null,
    archived: false,
    unread: false,
    preview: null,
    message_count: 0,
    created_at: iso(0),
    updated_at: iso(updated),
    ...extra,
  };
}

function msg(partial: Partial<Message> & { role: Message['role'] }): Message {
  return {
    id: null,
    conversation_id: CONV,
    run_id: RUN,
    content: '',
    reasoning: null,
    tool_calls: [],
    tool_call_id: null,
    name: null,
    partial: false,
    created_at: iso(0),
    ...partial,
  };
}

function withOpen(id: string | null, base: ChatState = initialChatState()): ChatState {
  return { ...base, openConversationId: id, conversations: { [CONV]: conv(CONV, 0), conv_b: conv('conv_b', 0) } };
}

const queued = runScoped({ type: 'run.queued', kind: 'chat', input_preview: 'hi', user_message_id: 'm_u1' });
const started = runScoped({ type: 'run.started' }, 10);
const modelCall = runScoped(
  { type: 'model.call', role: 'chat', provider: 'ollama', model: 'q', message_count: 3, tool_count: 4, think: true },
  20,
);

describe('applyServerEvent — streaming', () => {
  it('accumulates text and reasoning deltas into the run stream', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    s = applyServerEvent(s, runScoped({ type: 'model.delta', kind: 'reasoning', text: 'Let me ' }), T0 + 100);
    s = applyServerEvent(s, runScoped({ type: 'model.delta', kind: 'reasoning', text: 'think.' }), T0 + 200);
    s = applyServerEvent(s, runScoped({ type: 'model.delta', kind: 'text', text: 'Hel' }), T0 + 300);
    s = applyServerEvent(s, runScoped({ type: 'model.delta', kind: 'text', text: 'lo' }), T0 + 400);
    const stream = s.streams[RUN];
    expect(stream?.text).toBe('Hello');
    expect(stream?.reasoning).toBe('Let me think.');
    expect(stream?.reasoningStartedAt).toBe(T0 + 100);
    expect(stream?.reasoningEndedAt).toBe(T0 + 300);
    expect(stream?.modelActive).toBe(true);
    expect(s.runs[RUN]?.status).toBe('running');
    // deltas are never kept as events
    expect(s.runEvents[RUN]?.some((e) => e.type === 'model.delta')).toBe(false);
  });

  it('message.created replaces the stream buffer with the persisted assistant message', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    s = applyServerEvent(s, runScoped({ type: 'model.delta', kind: 'text', text: 'Hello there' }), T0);
    s = applyServerEvent(
      s,
      runScoped({ type: 'model.done', usage: usage(10, 5), finish_reason: 'stop', tool_call_count: 0 }, 50),
      T0,
    );
    const final = msg({ id: 'm_a1', role: 'assistant', content: 'Hello there', reasoning: 'r', created_at: iso(60) });
    s = applyServerEvent(s, { type: 'message.created', ts: iso(60), message: final }, T0);
    expect(s.messages[CONV]).toHaveLength(1);
    expect(s.messages[CONV]?.[0]?.id).toBe('m_a1');
    expect(s.streams[RUN]?.text).toBe('');
    expect(s.streams[RUN]?.modelActive).toBe(false);
    // the transcript shows the message once and no duplicate stream text
    const items = buildTranscript(s, CONV);
    expect(items.filter((i) => i.kind === 'message')).toHaveLength(1);
  });

  it('run.done removes the stream and marks the run terminal', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    s = applyServerEvent(s, runScoped({ type: 'run.done', message_id: 'm', usage: usage(), steps_used: 1, summary: null }), T0);
    expect(s.streams[RUN]).toBeUndefined();
    expect(s.runs[RUN]?.status).toBe('done');
    expect(selectActiveRun(s, CONV)).toBeNull();
  });
});

describe('applyServerEvent — cancel', () => {
  it('run.cancelled keeps the partial text as a partial assistant message and marks the run cancelled', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    s = applyServerEvent(s, runScoped({ type: 'model.delta', kind: 'text', text: 'Half an ans' }), T0);
    s = { ...s, cancelRequested: { [RUN]: true } };
    s = applyServerEvent(s, runScoped({ type: 'run.cancelled', partial_message_id: 'm_p1' }, 500), T0);
    expect(s.runs[RUN]?.status).toBe('cancelled');
    expect(s.streams[RUN]).toBeUndefined();
    expect(s.cancelRequested[RUN]).toBeUndefined();
    const partial = s.messages[CONV]?.find((m) => m.id === 'm_p1');
    expect(partial?.partial).toBe(true);
    expect(partial?.content).toBe('Half an ans');
    // the server's own copy replaces ours instead of duplicating it
    const persisted = msg({ id: 'm_p1', role: 'assistant', content: 'Half an ans', partial: true });
    s = applyServerEvent(s, { type: 'message.created', ts: iso(600), message: persisted }, T0);
    expect(s.messages[CONV]?.filter((m) => m.id === 'm_p1')).toHaveLength(1);
    const items = buildTranscript(s, CONV);
    expect(items.some((i) => i.kind === 'note' && i.note.tag === 'stopped')).toBe(true);
  });

  it('does not show Stopped before run.cancelled arrives', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    s = { ...s, cancelRequested: { [RUN]: true } };
    expect(s.runs[RUN]?.status).toBe('running');
    const items = buildTranscript(s, CONV);
    expect(items.some((i) => i.kind === 'note' && i.note.tag === 'stopped')).toBe(false);
  });
});

describe('applyServerEvent — waiting for the user', () => {
  it('tracks the pending confirmation until it is resolved and the run resumes', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    s = applyServerEvents(
      s,
      [
        runScoped(
          { type: 'tool.confirm_requested', call_id: 'c1', name: 'outlook.send', arguments: { to: 'x' }, reason: 'destructive' },
          100,
        ),
        runScoped({ type: 'run.waiting_user', reason: 'Confirm outlook.send', call_id: 'c1' }, 101),
      ],
      T0,
    );
    expect(s.runs[RUN]?.status).toBe('waiting_user');
    expect(s.runs[RUN]?.waiting_reason).toBe('Confirm outlook.send');
    expect(selectPendingConfirm(s, RUN)?.call_id).toBe('c1');
    expect(buildTranscript(s, CONV).some((i) => i.kind === 'confirm' && i.callId === 'c1')).toBe(true);

    s = applyServerEvents(
      s,
      [
        runScoped({ type: 'tool.confirm_resolved', call_id: 'c1', approved: true, note: null }, 200),
        runScoped({ type: 'run.resumed', from_seq: 5 }, 201),
        runScoped(
          { type: 'tool.call', call_id: 'c1', name: 'outlook.send', arguments: { to: 'x' }, read_only: false, idempotency_key: 'k' },
          202,
        ),
      ],
      T0,
    );
    expect(selectPendingConfirm(s, RUN)).toBeNull();
    expect(s.runs[RUN]?.status).toBe('running');
    const items = buildTranscript(s, CONV);
    expect(items.some((i) => i.kind === 'confirm')).toBe(false);
    const card = items.find((i) => i.kind === 'tool');
    expect(card?.kind === 'tool' && card.card.confirm?.approved).toBe(true);
  });
});

describe('applyServerEvent — conversations', () => {
  it('inserts and sorts conversations newest first on conversation.updated', () => {
    let s = initialChatState();
    s = applyServerEvent(s, { type: 'conversation.updated', ts: iso(0), conversation: conv('old', 1000) }, T0);
    s = applyServerEvent(s, { type: 'conversation.updated', ts: iso(0), conversation: conv('new', 2000) }, T0);
    expect(selectSidebar(s.conversations).chats.map((c) => c.id)).toEqual(['new', 'old']);
    // bumping the older one moves it to the top
    s = applyServerEvent(s, { type: 'conversation.updated', ts: iso(0), conversation: conv('old', 3000, { title: 'Renamed' }) }, T0);
    const chats = selectSidebar(s.conversations).chats;
    expect(chats.map((c) => c.id)).toEqual(['old', 'new']);
    expect(chats[0]?.title).toBe('Renamed');
  });

  it('groups non-chat kinds into folders and sub-groups by folder_label ?? folder_key', () => {
    let s = initialChatState();
    const evs: ServerEvent[] = [
      { type: 'conversation.updated', ts: iso(0), conversation: conv('s1', 10, { kind: 'scheduled', folder_key: 'sch_1', folder_label: 'Morning brief' }) },
      { type: 'conversation.updated', ts: iso(0), conversation: conv('s2', 20, { kind: 'scheduled', folder_key: 'sch_1', folder_label: 'Morning brief' }) },
      { type: 'conversation.updated', ts: iso(0), conversation: conv('t1', 30, { kind: 'triage', folder_key: '2026-09-05' }) },
      { type: 'conversation.updated', ts: iso(0), conversation: conv('c1', 40, { archived: true }) },
    ];
    s = applyServerEvents(s, evs, T0);
    const { chats, folders } = selectSidebar(s.conversations);
    expect(chats).toHaveLength(0);
    expect(folders.map((f) => f.kind)).toEqual(['scheduled', 'triage', 'archive']);
    expect(folders[0]?.groups[0]?.label).toBe('Morning brief');
    expect(folders[0]?.groups[0]?.conversations.map((c) => c.id)).toEqual(['s2', 's1']);
    expect(folders[1]?.groups[0]?.label).toBe('2026-09-05');
  });

  it('switches to the conversation the server created for a null-conversation send', () => {
    let s = initialChatState();
    const optimistic: LocalMessage = { ...msg({ role: 'user', content: 'hello', conversation_id: null, run_id: null }), optimistic: true };
    s = { ...s, pendingNewConversation: { clientRef: 'ref1', text: 'hello' }, messages: { __new__: [optimistic] } };
    s = applyServerEvent(s, { type: 'conversation.updated', ts: iso(0), conversation: conv('conv_new', 5) }, T0);
    expect(s.openConversationId).toBe('conv_new');
    expect(s.pendingNewConversation).toBeNull();
    expect(s.messages.__new__).toBeUndefined();
    expect(s.messages.conv_new?.[0]?.content).toBe('hello');
  });

  it('conversation.deleted drops the conversation, its messages and runs', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started], T0);
    s = applyServerEvent(s, { type: 'conversation.deleted', ts: iso(0), conversation_id: CONV }, T0);
    expect(s.conversations[CONV]).toBeUndefined();
    expect(s.runs[RUN]).toBeUndefined();
    expect(s.openConversationId).toBeNull();
  });
});

describe('applyServerEvent — unread', () => {
  it('marks a conversation unread on run.done when it is not the open one', () => {
    let s = withOpen('conv_b');
    s = applyServerEvents(s, [queued, started], T0);
    s = applyServerEvent(s, runScoped({ type: 'run.done', message_id: null, usage: usage(), steps_used: 1, summary: null }), T0);
    expect(s.conversations[CONV]?.unread).toBe(true);
    expect(s.conversations.conv_b?.unread).toBe(false);
  });

  it('leaves the open conversation read on run.done', () => {
    let s = withOpen(CONV);
    s = applyServerEvents(s, [queued, started], T0);
    s = applyServerEvent(s, runScoped({ type: 'run.done', message_id: null, usage: usage(), steps_used: 1, summary: null }), T0);
    expect(s.conversations[CONV]?.unread).toBe(false);
  });
});

describe('applyServerEvent — optimistic user message', () => {
  it('binds the optimistic message to the run and dedupes it against message.created', () => {
    const optimistic: LocalMessage = { ...msg({ role: 'user', content: 'hi', run_id: null, id: 'local_1' }), optimistic: true };
    let s: ChatState = { ...withOpen(CONV), messages: { [CONV]: [optimistic] } };
    s = applyServerEvent(s, queued, T0);
    expect(s.messages[CONV]?.[0]?.run_id).toBe(RUN);
    expect(s.messages[CONV]?.[0]?.id).toBe('m_u1');
    const persisted = msg({ id: 'm_u1', role: 'user', content: 'hi' });
    s = applyServerEvent(s, { type: 'message.created', ts: iso(1), message: persisted }, T0);
    expect(s.messages[CONV]).toHaveLength(1);
    expect(s.messages[CONV]?.[0]?.optimistic).toBeUndefined();
  });
});

describe('applyServerEvent — run.updated', () => {
  it('replays a non-terminal run after reconnect with a placeholder stream, and clears it when terminal', () => {
    const run: Run = {
      id: RUN,
      conversation_id: CONV,
      kind: 'chat',
      status: 'running',
      input_text: 'hi',
      plan: null,
      budget: { max_steps: 25, max_tokens: 1, max_seconds: 1 },
      priority: 0,
      steps_used: 2,
      usage: usage(),
      last_seq: 9,
      error: null,
      waiting_reason: null,
      think: null,
      think_level: null,
      created_at: iso(0),
      started_at: iso(1),
      finished_at: null,
    };
    let s = applyServerEvent(withOpen(CONV), { type: 'run.updated', ts: iso(2), run }, T0);
    expect(s.runsByConversation[CONV]).toEqual([RUN]);
    expect(s.streams[RUN]).toBeDefined();
    expect(selectActiveRun(s, CONV)?.id).toBe(RUN);
    s = applyServerEvent(s, { type: 'run.updated', ts: iso(3), run: { ...run, status: 'done' } }, T0);
    expect(s.streams[RUN]).toBeUndefined();
  });
});

describe('buildTranscript — tool cards', () => {
  it('merges the assistant tool_calls, tool.call/tool.result events and the tool message into one card', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started, modelCall], T0);
    const user = msg({ id: 'm_u1', role: 'user', content: 'hi', created_at: iso(0) });
    const assistant = msg({
      id: 'm_a1',
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'c1', name: 'notes.list', arguments: { board: 'x' } }],
      created_at: iso(50),
    });
    s = applyServerEvents(
      s,
      [
        { type: 'message.created', ts: iso(0), message: user },
        { type: 'message.created', ts: iso(50), message: assistant },
        runScoped({ type: 'tool.call', call_id: 'c1', name: 'notes.list', arguments: { board: 'x' }, read_only: true, idempotency_key: 'k' }, 60),
        runScoped(
          {
            type: 'tool.result',
            call_id: 'c1',
            name: 'notes.list',
            result: { kind: 'data', text: '3 notes', count: 3, total: 3, cursor: null, error: null },
            duration_ms: 42,
          },
          110,
        ),
        { type: 'message.created', ts: iso(111), message: msg({ id: 'm_t1', role: 'tool', tool_call_id: 'c1', name: 'notes.list', content: '3 notes', created_at: iso(111) }) },
      ],
      T0,
    );
    const items = buildTranscript(s, CONV);
    const cards = items.filter((i) => i.kind === 'tool');
    expect(cards).toHaveLength(1);
    const card = cards[0];
    if (card?.kind !== 'tool') throw new Error('expected tool card');
    expect(card.card.result?.kind).toBe('data');
    expect(card.card.durationMs).toBe(42);
    expect(card.card.readOnly).toBe(true);
    expect(card.card.pending).toBe(false);
    // order: user, assistant, tool card, stream (run still active), run chip
    expect(items.map((i) => i.kind)).toEqual(['message', 'message', 'tool', 'stream', 'run']);
  });

  it('keys tool cards by run so identical call ids in different runs never collide', () => {
    const RUN2 = 'run_2';
    let s = applyServerEvents(withOpen(CONV), [queued, started], T0);
    s = applyServerEvents(
      s,
      [
        runScoped({ type: 'tool.call', call_id: 'c1', name: 'notes.list', arguments: {}, read_only: true, idempotency_key: 'k' }, 10),
        runScoped({ type: 'run.done', message_id: null, usage: usage(), steps_used: 1, summary: null }, 20),
        { ...(queued as object), run_id: RUN2, ts: iso(30) } as ServerEvent,
        { ...(runScoped({ type: 'tool.call', call_id: 'c1', name: 'notes.list', arguments: {}, read_only: true, idempotency_key: 'k' }, 40) as object), run_id: RUN2 } as ServerEvent,
      ],
      T0,
    );
    const keys = buildTranscript(s, CONV).map((i) => i.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys.filter((k) => k.startsWith('tool:'))).toHaveLength(2);
  });

  it('shows which skills a run is using as a note', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started], T0);
    s = applyServerEvent(s, runScoped({ type: 'context.skills', names: ['email-triage', 'meeting-prep'] }, 5), T0);
    const note = buildTranscript(s, CONV).find((i) => i.kind === 'note');
    expect(note?.kind === 'note' && note.note.title).toBe('Using skills: email-triage, meeting-prep');
  });

  it('inserts the compaction divider right after up_to_message_id', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started], T0);
    s = applyServerEvents(
      s,
      [
        { type: 'message.created', ts: iso(1), message: msg({ id: 'm_u1', role: 'user', content: 'hi', created_at: iso(1) }) },
        { type: 'message.created', ts: iso(2), message: msg({ id: 'm_a1', role: 'assistant', content: 'hello', created_at: iso(2) }) },
        runScoped({ type: 'run.done', message_id: 'm_a1', usage: usage(), steps_used: 1, summary: null }, 3),
      ],
      T0,
    );
    const src = { ...transcriptSource(s, CONV), summary: { up_to_message_id: 'm_a1', text: 'Earlier: greetings.' } };
    const kinds = buildTranscriptFrom(src).map((i) => (i.kind === 'message' ? `${i.kind}:${i.message.id}` : i.kind));
    expect(kinds).toEqual(['message:m_u1', 'message:m_a1', 'summary', 'run']);
  });

  it('keeps the run plan current from plan.* events', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started], T0);
    const plan = { goal: 'Ship it', steps: [{ title: 'a', status: 'pending' as const, note: null }, { title: 'b', status: 'pending' as const, note: null }] };
    s = applyServerEvents(
      s,
      [
        runScoped({ type: 'plan.created', plan }, 1),
        runScoped({ type: 'plan.step_started', index: 0, title: 'a' }, 2),
        runScoped({ type: 'plan.step_done', index: 0 }, 3),
        runScoped({ type: 'plan.step_started', index: 1, title: 'b' }, 4),
      ],
      T0,
    );
    expect(s.runs[RUN]?.plan?.steps.map((st) => st.status)).toEqual(['done', 'in_progress']);
    // plan events are not rendered as separate notes — the checklist under the run chip shows them
    expect(buildTranscript(s, CONV).some((i) => i.kind === 'note')).toBe(false);
  });

  it('renders a judge stop as an error note with its reason', () => {
    let s = applyServerEvents(withOpen(CONV), [queued, started], T0);
    s = applyServerEvent(s, runScoped({ type: 'judge.verdict', verdict: 'stop', reason: 'Looping on the same call' }, 100), T0);
    const note = buildTranscript(s, CONV).find((i) => i.kind === 'note');
    expect(note?.kind === 'note' && note.note.level).toBe('error');
    expect(note?.kind === 'note' && note.note.detail).toBe('Looping on the same call');
  });
});
