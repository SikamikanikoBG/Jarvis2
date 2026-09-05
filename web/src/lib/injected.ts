import type { Conversation, Message } from '../protocol/types';

/**
 * User-role messages the core injects for the model (kept as real messages for prompt-cache
 * stability). They are never Arsen's words and must never render as his bubble.
 */
export const INJECTED_LABELS: Readonly<Record<string, string>> = {
  context: 'Context injected',
  plan: 'Plan injected',
  supervisor: 'Supervisor note',
  summary: 'Earlier turns summarised',
  transcript: 'Transcript',
  frame: 'Screen frame',
  triage: 'Triage batch',
};

export const CONTEXT_MARKER = '[Context for the request above]';

export function isInjectedUserMessage(m: Pick<Message, 'role' | 'name' | 'content'>): boolean {
  if (m.role !== 'user') return false;
  if (m.name !== null && m.name in INJECTED_LABELS) return true;
  // Defensive: a context block that lost its name still must not read as Arsen's message.
  return m.content.startsWith(CONTEXT_MARKER);
}

export function injectedLabel(name: string | null): string {
  return (name !== null ? INJECTED_LABELS[name] : undefined) ?? 'Injected';
}

/**
 * Sidebar preview: the server's, unless it is an injected context block; then the newest loaded
 * message that is neither injected nor a tool/system message — or nothing rather than the wrong thing.
 */
export function previewFor(c: Pick<Conversation, 'preview'>, messages: readonly Pick<Message, 'role' | 'name' | 'content'>[] | undefined): string | null {
  if (c.preview && !c.preview.startsWith(CONTEXT_MARKER)) return c.preview;
  for (let i = (messages?.length ?? 0) - 1; i >= 0; i--) {
    const m = messages?.[i];
    if (m && m.role !== 'tool' && m.role !== 'system' && !isInjectedUserMessage(m) && m.content) return m.content;
  }
  return null;
}
