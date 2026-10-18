import { describe, expect, it } from 'vitest';
import type { Message, Run, RunScopedEvent } from '../protocol/types';
import type { LocalMessage } from './state';
import { buildTranscriptFrom, type TranscriptItem, type TranscriptSource } from './transcript';

const T0 = Date.parse('2026-10-18T10:00:00.000Z');
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString();
const RUN = 'run_1';

function msg(partial: Partial<Message> & { role: Message['role'] }): LocalMessage {
  return {
    id: null,
    conversation_id: 'conv_a',
    run_id: RUN,
    content: '',
    reasoning: null,
    tool_calls: [],
    tool_call_id: null,
    name: null,
    partial: false,
    attachments: [],
    channel: 'text',
    created_at: iso(0),
    ...partial,
  };
}

function run(status: Run['status']): Run {
  return {
    id: RUN,
    conversation_id: 'conv_a',
    kind: 'chat',
    status,
    input_text: 'check it',
    plan: null,
    budget: { max_steps: 40, max_tokens: 200_000, max_seconds: 900 },
    priority: 0,
    steps_used: 2,
    usage: { prompt_tokens: 0, completion_tokens: 0, calls: 0, ttft_ms: 0, duration_ms: 0, cached_tokens: 0 },
    last_seq: 0,
    error: null,
    waiting_reason: null,
    think: null,
    think_level: null,
    call_me: false,
    call_me_default: false,
    created_at: iso(0),
    started_at: iso(0),
    finished_at: status === 'running' ? null : iso(9),
  };
}

const call = (id: string, name = 'web.search') => ({ id, name, arguments: { query: id } });

/** Arsen asks; Jarvis thinks, says what it will do, calls a tool, then answers (with its own thought). */
function oneToolTurn(): LocalMessage[] {
  return [
    msg({ id: 'm_u', role: 'user', content: 'check it', created_at: iso(1) }),
    msg({ id: 'm_a1', role: 'assistant', content: 'Let me look.', reasoning: 'need a search', tool_calls: [call('c1')], created_at: iso(2) }),
    msg({ id: 'm_t1', role: 'tool', content: 'found it', tool_call_id: 'c1', name: 'web.search', created_at: iso(3) }),
    msg({ id: 'm_a2', role: 'assistant', content: 'Here it is.', reasoning: 'summarise', created_at: iso(4) }),
  ];
}

function source(messages: LocalMessage[], status: Run['status'], extra: Partial<TranscriptSource> = {}): TranscriptSource {
  return { messages, runIds: [RUN], runs: { [RUN]: run(status) }, runEvents: {}, streamRunIds: new Set(), foldWork: true, ...extra };
}

const shape = (items: TranscriptItem[]) =>
  items.map((i) => (i.kind === 'message' ? `message:${i.message.id}` : i.kind === 'work' ? `work(${i.items.map((w) => w.kind).join(',')})` : i.kind));

describe('folding a finished run’s work', () => {
  it('leaves his words and the answer in sight and folds the tools and thinking in between', () => {
    const items = buildTranscriptFrom(source(oneToolTurn(), 'done'));
    expect(shape(items)).toEqual(['message:m_u', 'work(message,tool,message)', 'message:m_a2', 'run']);
    const work = items[1];
    if (work?.kind !== 'work') throw new Error('no work item');
    expect(work.steps).toBe(1);
    expect(work.errors).toBe(0);
    // The answer's own reasoning goes into the fold too: a finished reply is text and files only.
    const answer = items[2];
    expect(answer?.kind === 'message' && answer.message.reasoning).toBeFalsy();
    const thought = work.items.at(-1);
    expect(thought?.kind === 'message' ? thought.message.reasoning : null).toBe('summarise');
  });

  it('keeps a run that is still working exactly as it is now', () => {
    const items = buildTranscriptFrom(source(oneToolTurn().slice(0, 3), 'running'));
    expect(items.some((i) => i.kind === 'work')).toBe(false);
    expect(items.some((i) => i.kind === 'tool')).toBe(true);
  });

  it('keeps every answer of a steered call in sight, each with its own fold', () => {
    const messages = [
      msg({ id: 'm_u', role: 'user', content: 'debrief me', channel: 'voice', created_at: iso(1) }),
      msg({ id: 'm_a1', role: 'assistant', content: '', tool_calls: [call('c1', 'sessions.today')], created_at: iso(2) }),
      msg({ id: 'm_t1', role: 'tool', content: '[]', tool_call_id: 'c1', name: 'sessions.today', created_at: iso(3) }),
      msg({ id: 'm_r1', role: 'assistant', content: 'Short version…', channel: 'voice', created_at: iso(4) }),
      msg({ id: 'm_s', role: 'user', name: 'supervisor', content: '[supervisor] He went on talking.', created_at: iso(5) }),
      msg({ id: 'm_r2', role: 'assistant', content: 'Both, then…', channel: 'voice', created_at: iso(6) }),
    ];
    const items = buildTranscriptFrom(source(messages, 'done'));
    expect(shape(items)).toEqual(['message:m_u', 'work(message,tool)', 'message:m_r1', 'work(injected)', 'message:m_r2', 'run']);
  });

  it('counts the failed tools on the folded row and leaves the run’s outcome outside it', () => {
    const messages = oneToolTurn().slice(0, 3);
    const events = [
      { type: 'tool.call', ts: iso(2), run_id: RUN, conversation_id: 'conv_a', seq: 1, call_id: 'c1', name: 'web.search', arguments: {}, read_only: true, idempotency_key: 'k' },
      {
        type: 'tool.result',
        ts: iso(3),
        run_id: RUN,
        conversation_id: 'conv_a',
        seq: 2,
        call_id: 'c1',
        name: 'web.search',
        duration_ms: 10,
        result: { kind: 'error', text: 'boom', count: null, total: null, cursor: null, error: 'boom' },
      },
      { type: 'run.failed', ts: iso(5), run_id: RUN, conversation_id: 'conv_a', seq: 3, error: 'ConnectTimeout' },
    ] as unknown as RunScopedEvent[];
    const items = buildTranscriptFrom(source(messages, 'failed', { runEvents: { [RUN]: events } }));
    const work = items.find((i) => i.kind === 'work');
    if (work?.kind !== 'work') throw new Error('no work item');
    expect(work.errors).toBe(1);
    expect(items.some((i) => i.kind === 'note' && i.note.tag === 'failed')).toBe(true);
  });

  it('changes nothing when folding is off', () => {
    const items = buildTranscriptFrom(source(oneToolTurn(), 'done', { foldWork: false }));
    expect(shape(items)).toEqual(['message:m_u', 'message:m_a1', 'tool', 'message:m_a2', 'run']);
  });
});
