import { describe, expect, it } from 'vitest';
import type { Conversation } from '../protocol/types';
import type { LocalMessage } from '../store/state';
import { CONTEXT_MARKER, injectedLabel, isInjectedUserMessage, previewFor } from './injected';

const conv = (preview: string | null): Conversation => ({
  id: 'c',
  kind: 'chat',
  title: 't',
  folder_key: null,
  folder_label: null,
  archived: false,
  unread: false,
  preview,
  message_count: 0,
  created_at: '2026-09-05T10:00:00.000Z',
  updated_at: '2026-09-05T10:00:00.000Z',
});

const m = (role: LocalMessage['role'], content: string, name: string | null = null): LocalMessage => ({
  id: null,
  conversation_id: 'c',
  run_id: null,
  role,
  content,
  reasoning: null,
  tool_calls: [],
  tool_call_id: null,
  name,
  partial: false,
  created_at: '2026-09-05T10:00:00.000Z',
});

describe('isInjectedUserMessage', () => {
  it('recognises the injected names and the bare context marker, and nothing else', () => {
    for (const name of ['context', 'plan', 'supervisor', 'summary', 'transcript', 'frame', 'triage']) expect(isInjectedUserMessage(m('user', 'x', name))).toBe(true);
    expect(isInjectedUserMessage(m('user', `${CONTEXT_MARKER}\nfacts`))).toBe(true);
    expect(isInjectedUserMessage(m('user', 'hello'))).toBe(false);
    expect(isInjectedUserMessage(m('user', 'hello', 'arsen'))).toBe(false);
    expect(isInjectedUserMessage(m('assistant', CONTEXT_MARKER))).toBe(false);
  });

  it('labels known names and falls back for unknown ones', () => {
    expect(injectedLabel('context')).toBe('Context injected');
    expect(injectedLabel('frame')).toBe('Screen frame');
    expect(injectedLabel('something-else')).toBe('Injected');
    expect(injectedLabel(null)).toBe('Injected');
  });
});

describe('sidebar previewFor', () => {
  it('keeps a normal server preview', () => {
    expect(previewFor(conv('Three things stand out'), undefined)).toBe('Three things stand out');
  });

  it('skips a context-block preview in favour of the newest real message', () => {
    const msgs = [m('user', 'What is on my plate?'), m('user', `${CONTEXT_MARKER} skills…`, 'context'), m('tool', 'raw result')];
    expect(previewFor(conv(`${CONTEXT_MARKER} skills…`), msgs)).toBe('What is on my plate?');
  });

  it('shows nothing rather than the context block when no other message is loaded', () => {
    expect(previewFor(conv(`${CONTEXT_MARKER} skills…`), undefined)).toBeNull();
    expect(previewFor(conv(`${CONTEXT_MARKER} skills…`), [m('user', CONTEXT_MARKER, 'context')])).toBeNull();
  });
});
