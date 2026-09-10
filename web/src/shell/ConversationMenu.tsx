import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { InstructionsDialog } from '../chat/InstructionsDialog';
import { IconButton, InlineConfirm, Menu, type MenuItem } from '../components/primitives';
import { TTL_CHOICES, ttlLabel } from '../lib/privacy';
import { useStore } from '../store/store';

/**
 * "⋯" for the OPEN conversation, in the top bar: rename / archive / delete.
 * The sidebar row has the same menu, but on a phone the sidebar is a drawer and the row's
 * button is easy to miss - this one is always in reach next to the title.
 */
export function ConversationMenu() {
  const id = useStore((s) => s.openConversationId);
  const conv = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId] : undefined));
  const rename = useStore((s) => s.renameConversation);
  const archive = useStore((s) => s.archiveConversation);
  const remove = useStore((s) => s.deleteConversation);
  const pin = useStore((s) => s.pinConversation);
  const exportConv = useStore((s) => s.exportConversation);
  const setInstructions = useStore((s) => s.setConversationInstructions);
  const setTtl = useStore((s) => s.setConversationTtl);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  // Two-level like the sidebar row: the actions, and the idle-time picker swaps in.
  const [menuMode, setMenuMode] = useState<'main' | 'ttl'>('main');
  const [confirm, setConfirm] = useState(false);
  const [editing, setEditing] = useState(false);
  const [instructing, setInstructing] = useState(false);
  const [title, setTitle] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);
  // Rendered with key={conversation id} by the top bar, so switching conversations remounts it
  // and drops any open menu / confirm / rename state without an effect.

  if (!id || !conv) return null;

  const commit = () => {
    setEditing(false);
    const t = title.trim();
    if (t && t !== conv.title) void rename(id, t);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') commit();
    if (e.key === 'Escape') setEditing(false);
  };

  if (confirm) {
    return (
      <InlineConfirm
        text={`Delete "${conv.title}"?`}
        confirmLabel="Delete"
        danger
        onConfirm={() => {
          setConfirm(false);
          void remove(id);
        }}
        onCancel={() => setConfirm(false)}
      />
    );
  }
  if (editing) {
    return <input ref={inputRef} className="input conv-rename" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={commit} onKeyDown={onKey} aria-label="Conversation title" />;
  }
  const mainItems: MenuItem[] = [
    {
      label: 'Rename',
      icon: 'edit',
      onSelect: () => {
        setTitle(conv.title);
        setEditing(true);
      },
    },
    {
      label: conv.instructions.trim() ? 'Instructions ✓' : 'Instructions…',
      icon: 'brain',
      onSelect: () => setInstructing(true),
    },
    { label: conv.pinned ? 'Unpin' : 'Pin', icon: 'pin', onSelect: () => void pin(id, !conv.pinned) },
    ...(conv.kind === 'chat'
      ? [
          {
            label: conv.ttl_seconds === null ? 'Disappear after…' : `Disappears after ${ttlLabel(conv.ttl_seconds)}…`,
            icon: 'hourglass' as const,
            keepOpen: true,
            onSelect: () => setMenuMode('ttl'),
          },
        ]
      : []),
    { label: conv.archived ? 'Unarchive' : 'Archive', icon: conv.archived ? 'unarchive' : 'archive', onSelect: () => void archive(id, !conv.archived) },
    { label: 'Export as Markdown', icon: 'download', onSelect: () => void exportConv(id, 'markdown') },
    { label: 'Export as JSON', icon: 'download', onSelect: () => void exportConv(id, 'json') },
    { label: 'Delete', icon: 'trash', danger: true, onSelect: () => setConfirm(true) },
  ];
  const ttlItems: MenuItem[] = [
    { label: 'Keep this chat', ...(conv.ttl_seconds === null ? { icon: 'check' as const } : {}), onSelect: () => void setTtl(id, null) },
    ...TTL_CHOICES.map(
      (t): MenuItem => ({
        label: `After ${t.label} of quiet`,
        icon: conv.ttl_seconds === t.seconds ? 'check' : 'hourglass',
        onSelect: () => void setTtl(id, t.seconds),
      }),
    ),
  ];
  return (
    <>
      {instructing && (
        <InstructionsDialog
          conversation={conv}
          onClose={() => setInstructing(false)}
          onSave={(text) => void setInstructions(id, text)}
        />
      )}
      <IconButton
        icon="more"
        label="Conversation menu"
        aria-haspopup="menu"
        aria-expanded={Boolean(anchor)}
        onClick={(e) => {
          setMenuMode('main');
          setAnchor(anchor ? null : e.currentTarget);
        }}
      />
      {anchor && <Menu anchor={anchor} onClose={() => setAnchor(null)} items={menuMode === 'main' ? mainItems : ttlItems} />}
    </>
  );
}
