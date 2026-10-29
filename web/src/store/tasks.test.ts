import { describe, expect, it } from 'vitest';
import { dodMark, TASK_STATE_LABEL, taskTone } from '../lib/tasks';
import type { Conversation, Message, ServerEvent, Task, TaskState } from '../protocol/types';
import { applyServerEvent } from './reducer';
import { selectOpenTasks, selectSidebar, selectTasksFor } from './selectors';
import { initialChatState, type LocalMessage } from './state';
import { buildTranscriptFrom } from './transcript';

const T0 = Date.parse('2026-10-29T10:00:00.000Z');
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString();

function task(id: string, state: TaskState, extra: Partial<Task> = {}): Task {
  return {
    id,
    title: `Task ${id}`,
    state,
    brief: { goal: 'g', context: '', constraints: [], dod: [{ item: 'three options', outward: false }], approvals: [], needs: [], assumptions: [], asked: ['find hotels'], written: true },
    origin_conversation_id: 'conv_a',
    origin_run_id: 'run_1',
    call_id: null,
    conversation_id: `conv_${id}`,
    run_id: `run_${id}`,
    depth: 0,
    outcome: null,
    waiting: null,
    report_message_id: null,
    incognito: false,
    note: null,
    paused: false,
    created_at: iso(0),
    started_at: iso(1),
    finished_at: null,
    updated_at: iso(1),
    ...extra,
  };
}

function changed(t: Task): ServerEvent {
  return { type: 'task.changed', ts: t.updated_at, task: t };
}

function msg(partial: Partial<Message> & { role: Message['role'] }): LocalMessage {
  return {
    id: null,
    conversation_id: 'conv_a',
    run_id: 'run_1',
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

describe('task events', () => {
  it('task.changed upserts and leaves the runs alone', () => {
    const s0 = initialChatState();
    const s1 = applyServerEvent(s0, changed(task('t1', 'queued')), T0);
    expect(s1.tasks.t1?.state).toBe('queued');
    expect(s1.runs).toBe(s0.runs);
    const s2 = applyServerEvent(s1, changed(task('t1', 'running', { updated_at: iso(5) })), T0);
    expect(s2.tasks.t1?.state).toBe('running');
  });

  it('an older copy never puts a stale state back; the same instant does (paused moves unsaved)', () => {
    const s1 = applyServerEvent(initialChatState(), changed(task('t1', 'done', { updated_at: iso(9) })), T0);
    const stale = applyServerEvent(s1, changed(task('t1', 'running', { updated_at: iso(2) })), T0);
    expect(stale.tasks.t1?.state).toBe('done');
    const paused = applyServerEvent(s1, changed(task('t1', 'done', { updated_at: iso(9), paused: true })), T0);
    expect(paused.tasks.t1?.paused).toBe(true);
  });

  it('task.removed and a deleted chat take the cards away', () => {
    let s = applyServerEvent(initialChatState(), changed(task('t1', 'running')), T0);
    s = applyServerEvent(s, changed(task('t2', 'running', { origin_conversation_id: 'conv_b' })), T0);
    s = applyServerEvent(s, { type: 'task.removed', ts: iso(3), task_id: 't2' }, T0);
    expect(Object.keys(s.tasks)).toEqual(['t1']);
    s = applyServerEvent(s, { type: 'conversation.deleted', ts: iso(4), conversation_id: 'conv_a' }, T0);
    expect(s.tasks).toEqual({});
  });

  it('the chip counts this chat’s open tasks; dropped ones are not shown at all', () => {
    const tasks = {
      a: task('a', 'running'),
      b: task('b', 'done'),
      c: task('c', 'dropped'),
      d: task('d', 'waiting', { origin_conversation_id: 'conv_b' }),
    };
    expect(selectTasksFor(tasks, 'conv_a').map((t) => t.id)).toEqual(['a', 'b']);
    expect(selectOpenTasks(tasks, 'conv_a').map((t) => t.id)).toEqual(['a']);
    expect(selectOpenTasks(tasks, null)).toEqual([]);
  });
});

describe('task cards in the transcript', () => {
  const user = msg({ id: 'm1', role: 'user', content: 'find hotels and trains' });
  const ack = msg({ id: 'm2', role: 'assistant', content: 'On it.', created_at: iso(1) });
  const later = msg({ id: 'm3', run_id: 'run_2', role: 'user', content: 'thanks', created_at: iso(5) });
  const note = msg({ id: 'm4', run_id: null, role: 'user', name: 'task', task_id: 't1', content: '[core note, not from Arsen] ...', created_at: iso(9) });
  const report = msg({ id: 'm5', run_id: null, role: 'assistant', task_id: 't1', content: '**Task: Hotels — done**', created_at: iso(9) });

  it('goes right after the turn that handed it off, the report stays a message, the core note is not shown', () => {
    const items = buildTranscriptFrom({
      messages: [user, ack, later, note, report],
      runIds: [],
      runs: {},
      runEvents: {},
      streamRunIds: new Set(),
      tasks: [
        { id: 't1', originRunId: 'run_1' },
        { id: 't2', originRunId: 'run_1' },
      ],
    });
    const keys = items.map((i) => i.key);
    expect(keys.indexOf('task:t1')).toBe(keys.indexOf('msg:m2') + 1);
    expect(keys.indexOf('task:t2')).toBe(keys.indexOf('task:t1') + 1);
    expect(keys.indexOf('msg:m3')).toBeGreaterThan(keys.indexOf('task:t2'));
    expect(keys).toContain('msg:m5');
    expect(keys).not.toContain('msg:m4');
    expect(items.some((i) => i.kind === 'injected')).toBe(false);
  });

  it('a card whose turn is not loaded still shows, at the end', () => {
    const items = buildTranscriptFrom({ messages: [later], runIds: [], runs: {}, runEvents: {}, streamRunIds: new Set(), tasks: [{ id: 't9', originRunId: 'run_gone' }] });
    expect(items.at(-1)?.key).toBe('task:t9');
  });
});

describe('how a card reads', () => {
  it('says what the code verified, never what the model claimed', () => {
    const base = { item: 'x', outward: false, refs: [], note: null };
    expect(dodMark({ ...base, met: true, verified: true, why: null }).mark).toBe('✅');
    const claimed = dodMark({ ...base, met: true, verified: false, why: 'c9 is not a call of this task' });
    expect(claimed.mark).toBe('⚠️');
    expect(claimed.label).toContain('claimed, not verified');
    expect(dodMark({ ...base, met: false, verified: false, why: null, note: 'sold out' }).label).toBe('not met: sold out');
    expect(dodMark({ ...base, met: null, verified: false, why: null }).label).toBe('not reported');
  });

  it('every state has words and a tone', () => {
    for (const s of Object.keys(TASK_STATE_LABEL) as TaskState[]) {
      expect(TASK_STATE_LABEL[s]).toBeTruthy();
      expect(taskTone(s)).toBeTruthy();
    }
    expect(taskTone('partial')).toBe('wait');
    expect(taskTone('not_done')).toBe('fail');
  });
});

describe('the sidebar', () => {
  it('never lists a task’s own conversation', () => {
    const c = (id: string, kind: Conversation['kind']): Conversation =>
      ({ id, kind, title: id, folder_key: null, folder_label: null, folder_id: null, archived: false, unread: true, pinned: false, title_auto: true, instructions: '', preview: null, message_count: 1, activity: 'running', incognito: false, ttl_seconds: null, expires_at: null, created_at: iso(0), updated_at: iso(0) });
    const model = selectSidebar({ a: c('a', 'chat'), w: c('w', 'task') });
    expect(model.chats.map((x) => x.id)).toEqual(['a']);
    expect(model.folders).toEqual([]);
    expect(model.attention).toBe(1);
    expect(selectSidebar({ a: c('a', 'chat'), w: c('w', 'task') }, [], { attentionOnly: true }).chats.map((x) => x.id)).toEqual(['a']);
  });
});
