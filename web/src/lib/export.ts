import type { Conversation, Message } from '../protocol/types';
import { isInjectedUserMessage } from './injected';

/** A conversation as a readable Markdown transcript: user / Jarvis turns, tool turns folded to one line. */
export function conversationToMarkdown(conv: Conversation, messages: Message[]): string {
  const lines: string[] = [`# ${conv.title}`, '', `_Exported ${new Date().toISOString().slice(0, 16).replace('T', ' ')} · ${messages.length} messages_`, ''];
  for (const m of messages) {
    if (m.role === 'user') {
      if (isInjectedUserMessage(m)) continue; // context / plan / supervisor notes are not the conversation
      lines.push('## You', '', m.content.trim(), '');
    } else if (m.role === 'assistant') {
      if (m.tool_calls.length && !m.content) {
        lines.push(`> Jarvis called ${m.tool_calls.map((c) => `\`${c.name}\``).join(', ')}`, '');
        continue;
      }
      if (!m.content) continue;
      lines.push('## Jarvis', '', m.content.trim() + (m.partial ? '\n\n_(partial — stopped before the reply finished)_' : ''), '');
    } else if (m.role === 'tool') {
      const head = (m.content || '').split('\n')[0]?.slice(0, 120) ?? '';
      lines.push(`> Tool result${m.name ? ` (${m.name})` : ''}: ${head}${m.content.length > 120 ? '…' : ''}`, '');
    }
  }
  return lines.join('\n');
}

export function safeFilename(title: string): string {
  const s = title
    .normalize('NFKD')
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
  return s || 'conversation';
}

/** Trigger a file download from text (the SPA runs first-party, so blob URLs are allowed). */
export function downloadText(filename: string, text: string, mime: string): void {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Copy to the clipboard; resolves false when the browser refuses (no secure context, no permission). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Web Share on devices that have it (phones); false means "not available", the caller falls back to copy. */
export async function shareText(title: string, text: string): Promise<boolean> {
  if (!('share' in navigator)) return false;
  try {
    await navigator.share({ title, text });
    return true;
  } catch {
    return false; // dismissed or refused
  }
}
