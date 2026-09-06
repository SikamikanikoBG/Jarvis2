import { describe, expect, it } from 'vitest';
import type { Conversation, Message } from '../protocol/types';
import { conversationToMarkdown, safeFilename } from './export';

const conv: Conversation = {
  id: 'c1',
  kind: 'chat',
  title: 'Бюджет Q4 / plan',
  folder_key: null,
  folder_label: null,
  archived: false,
  unread: false,
  pinned: false,
  title_auto: true,
  preview: null,
  message_count: 0,
  created_at: '2026-09-06T08:00:00.000Z',
  updated_at: '2026-09-06T09:00:00.000Z',
};

const msg = (over: Partial<Message>): Message => ({
  id: 'm',
  conversation_id: 'c1',
  run_id: null,
  role: 'user',
  content: '',
  reasoning: null,
  tool_calls: [],
  tool_call_id: null,
  name: null,
  partial: false,
  created_at: '2026-09-06T08:00:00.000Z',
  ...over,
});

describe('conversationToMarkdown', () => {
  it('keeps the conversation and folds the machinery', () => {
    const md = conversationToMarkdown(conv, [
      msg({ id: 'm1', role: 'user', content: 'is the budget approved?' }),
      // Injected context is not part of the conversation and must not leak into an export.
      msg({ id: 'm2', role: 'user', content: '[Context] secret notes', name: 'context' }),
      msg({ id: 'm3', role: 'assistant', content: '', tool_calls: [{ id: 't1', name: 'outlook_list', arguments: {} }] }),
      msg({ id: 'm4', role: 'tool', content: 'a very long tool result\nsecond line', name: 'outlook_list' }),
      msg({ id: 'm5', role: 'assistant', content: 'Yes, 1.2M.' }),
      msg({ id: 'm6', role: 'assistant', content: 'Cut short', partial: true }),
    ]);
    expect(md).toContain('# Бюджет Q4 / plan');
    expect(md).toContain('## You\n\nis the budget approved?');
    expect(md).toContain('## Jarvis\n\nYes, 1.2M.');
    expect(md).not.toContain('secret notes');
    expect(md).toContain('> Jarvis called `outlook_list`');
    expect(md).toContain('> Tool result (outlook_list): a very long tool result');
    expect(md).toContain('_(partial — stopped before the reply finished)_');
  });
});

describe('safeFilename', () => {
  it('makes a filename out of any title', () => {
    expect(safeFilename('Бюджет Q4 / plan')).toBe('Бюджет-Q4-plan'); // Cyrillic titles keep their letters
    expect(safeFilename('   ')).toBe('conversation');
    expect(safeFilename('a'.repeat(200))).toHaveLength(60);
    expect(safeFilename('report: 2026/09/06 *final*')).toBe('report-2026-09-06-final');
  });
});
