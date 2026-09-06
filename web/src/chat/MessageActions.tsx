import { IconButton } from '../components/primitives';
import { copyText, shareText } from '../lib/export';
import type { LocalMessage } from '../store/state';
import { useStore } from '../store/store';
import { PinButton } from './PinButton';

/**
 * The one action bar every message gets (docs/WAVE2.md slice 0): copy, share, and per role
 * "edit and resend" (user → forks the chat) or "regenerate" (last reply) and pin to a board.
 */
export function MessageActions({ message }: { message: LocalMessage }) {
  const notify = useStore((s) => s.notify);
  const startEdit = useStore((s) => s.startEdit);
  const regenerate = useStore((s) => s.regenerate);
  const canShare = typeof navigator !== 'undefined' && 'share' in navigator;
  const isLastReply = useStore((s) => {
    if (message.role !== 'assistant' || !message.conversation_id) return false;
    const list = s.messages[message.conversation_id] ?? [];
    for (let i = list.length - 1; i >= 0; i--) {
      const m = list[i];
      if (m?.role === 'assistant' && m.content) return m.id === message.id;
    }
    return false;
  });
  const busy = useStore((s) => {
    const id = message.conversation_id;
    return id ? (s.runsByConversation[id] ?? []).some((r) => s.streams[r]) : false;
  });

  const copy = async () => {
    notify((await copyText(message.content)) ? 'Copied' : 'Copy failed — the browser refused clipboard access', 'info');
  };
  const share = async () => {
    if (!(await shareText('Jarvis', message.content))) await copy();
  };

  const messageId = message.id;
  const conversationId = message.conversation_id;
  if (!messageId || message.optimistic) return null;
  return (
    <div className="msg-actions" role="toolbar" aria-label="Message actions">
      <IconButton icon="copy" label="Copy" size="sm" className="msg-action" onClick={() => void copy()} />
      {canShare && <IconButton icon="share" label="Share" size="sm" className="msg-action" onClick={() => void share()} />}
      {message.role === 'user' && conversationId && (
        <IconButton
          icon="edit"
          label="Edit and resend (forks the chat)"
          size="sm"
          className="msg-action"
          onClick={() => startEdit(conversationId, messageId, message.content)}
        />
      )}
      {message.role === 'assistant' && isLastReply && (
        <IconButton icon="refresh" label="Regenerate" size="sm" className="msg-action" disabled={busy} onClick={regenerate} />
      )}
      {message.role === 'assistant' && <PinButton messageId={messageId} text={message.content} />}
    </div>
  );
}
