import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent } from 'react';
import { Icon } from '../components/Icon';
import { IconButton, InlineConfirm, Menu, RelativeTime, type MenuItem } from '../components/primitives';
import { stripMarkdown } from '../lib/format';
import { previewFor } from '../lib/injected';
import { TTL_CHOICES, timeLeft, ttlLabel } from '../lib/privacy';
import type { Conversation } from '../protocol/types';
import { useStore } from '../store/store';
import { ActivityDot } from './ActivityDot';
import { setDragIds } from './dnd';

interface Props {
  conversation: Conversation;
  active: boolean;
  /** The sidebar is multi-selecting: a click ticks the row instead of opening the chat. */
  selecting?: boolean;
  selected?: boolean;
  /** Every row id in the order they are shown, so shift-click knows what "in between" means. */
  ordered?: string[];
}

export function ConversationRow({ conversation: c, active, selecting = false, selected = false, ordered = [] }: Props) {
  const open = useStore((s) => s.openConversation);
  // An incognito row never quotes the chat - not from the server's preview (there is none) and
  // not from the messages this client has loaded either.
  const preview = useStore((s) => (c.incognito ? null : previewFor(c, s.messages[c.id])));
  const setTtl = useStore((s) => s.setConversationTtl);
  const rename = useStore((s) => s.renameConversation);
  const archive = useStore((s) => s.archiveConversation);
  const remove = useStore((s) => s.deleteConversation);
  const pin = useStore((s) => s.pinConversation);
  const exportConv = useStore((s) => s.exportConversation);
  const folders = useStore((s) => s.folders);
  const move = useStore((s) => s.moveConversation);
  const selection = useStore((s) => s.selection);
  const toggleSelected = useStore((s) => s.toggleSelected);
  const extendSelection = useStore((s) => s.extendSelection);
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  // The row menu is two-level: the actions, and a picker - folders for "Move to…", idle times
  // for "Disappear after…" - swaps in.
  const [menuMode, setMenuMode] = useState<'main' | 'move' | 'ttl'>('main');
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [title, setTitle] = useState(c.title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const commit = () => {
    setEditing(false);
    const t = title.trim();
    if (t && t !== c.title) void rename(c.id, t);
    else setTitle(c.title);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') commit();
    if (e.key === 'Escape') {
      setTitle(c.title);
      setEditing(false);
    }
  };

  /** Click: open the chat, unless we are picking chats — or the modifiers say we now are. */
  const onClick = (e: MouseEvent<HTMLButtonElement>) => {
    if (e.shiftKey && (selecting || selection.length > 0)) {
      extendSelection(c.id, ordered);
      return;
    }
    if (selecting || e.ctrlKey || e.metaKey) {
      toggleSelected(c.id);
      return;
    }
    void open(c.id);
  };

  const onDragStart = (e: DragEvent<HTMLDivElement>) => {
    // Dragging a row inside the selection takes the whole selection with it.
    setDragIds(e.dataTransfer, selection.includes(c.id) ? selection : [c.id]);
  };

  if (confirmDelete) {
    return (
      <div className="conv">
        <InlineConfirm text={`Delete "${c.title}"?`} confirmLabel="Delete" danger onConfirm={() => void remove(c.id)} onCancel={() => setConfirmDelete(false)} />
      </div>
    );
  }

  const archived = c.archived;
  // Only plain chats are Arsen's to file; a scheduled fire or a meeting belongs to its own folder,
  // and an incognito chat is about to leave anyway.
  const fileable = c.kind === 'chat' && !archived && !c.incognito;
  const moveItem: MenuItem = {
    label: c.folder_id ? 'Move to another folder…' : 'Move to folder…',
    icon: 'folder',
    keepOpen: true, // swaps this menu for the folder picker rather than closing it
    onSelect: () => setMenuMode('move'),
  };
  const ttlItem: MenuItem = {
    label: c.ttl_seconds === null ? 'Disappear after…' : `Disappears after ${ttlLabel(c.ttl_seconds)}…`,
    icon: 'hourglass',
    keepOpen: true,
    onSelect: () => setMenuMode('ttl'),
  };
  const mainItems: MenuItem[] = [
    { label: 'Select', icon: 'checkSquare', onSelect: () => toggleSelected(c.id) },
    { label: 'Rename', icon: 'edit', onSelect: () => setEditing(true) },
    { label: c.pinned ? 'Unpin' : 'Pin', icon: 'pin', onSelect: () => void pin(c.id, !c.pinned) },
    ...(fileable ? [moveItem] : []),
    ...(c.kind === 'chat' ? [ttlItem] : []),
    { label: archived ? 'Unarchive' : 'Archive', icon: archived ? 'unarchive' : 'archive', onSelect: () => void archive(c.id, !archived) },
    { label: 'Export as Markdown', icon: 'download', onSelect: () => void exportConv(c.id, 'markdown') },
    { label: 'Export as JSON', icon: 'download', onSelect: () => void exportConv(c.id, 'json') },
    { label: 'Delete', icon: 'trash', danger: true, onSelect: () => setConfirmDelete(true) },
  ];
  const moveItems: MenuItem[] = [
    ...folders
      .filter((f) => f.id !== c.folder_id)
      .map((f) => ({ label: f.name, icon: 'folder' as const, onSelect: () => void move(c.id, f.id) })),
    ...(c.folder_id ? [{ label: 'Out of every folder', icon: 'x' as const, onSelect: () => void move(c.id, null) }] : []),
    ...(folders.length === 0 ? [{ label: 'No folders yet — make one first', icon: 'info' as const, disabled: true, onSelect: () => undefined }] : []),
  ];
  const ttlItems: MenuItem[] = [
    { label: 'Keep this chat', ...(c.ttl_seconds === null ? { icon: 'check' as const } : {}), onSelect: () => void setTtl(c.id, null) },
    ...TTL_CHOICES.map(
      (t): MenuItem => ({
        label: `After ${t.label} of quiet`,
        icon: c.ttl_seconds === t.seconds ? 'check' : 'hourglass',
        onSelect: () => void setTtl(c.id, t.seconds),
      }),
    ),
  ];

  const cls = [
    'conv',
    active ? 'active' : '',
    c.unread ? 'unread' : '',
    menuAnchor ? 'menu-open' : '',
    selected ? 'selected' : '',
    c.incognito ? 'conv-incognito' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={cls} draggable={fileable && !editing} onDragStart={onDragStart}>
      {editing ? (
        <input ref={inputRef} className="input conv-rename" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={commit} onKeyDown={onKey} aria-label="Conversation title" />
      ) : (
        <>
          {selecting && (
            <span className={selected ? 'conv-check on' : 'conv-check'} aria-hidden="true">
              {selected && <Icon name="check" size={12} />}
            </span>
          )}
          <button
            type="button"
            className="conv-main"
            onClick={onClick}
            aria-current={active ? 'page' : undefined}
            aria-pressed={selecting ? selected : undefined}
            style={{ textAlign: 'left', color: 'inherit' }}
          >
            <div className="conv-title">
              <ActivityDot activity={c.activity} />
              {c.unread && <span className="unread-dot" aria-label="Unread" />}
              {c.pinned && <Icon name="pin" size={12} className="conv-pin" aria-label="Pinned" />}
              {c.incognito ? (
                <Icon name="incognito" size={12} className="conv-privacy" aria-label="Incognito" />
              ) : (
                c.expires_at && <Icon name="hourglass" size={12} className="conv-privacy" aria-label={`Disappears in ${timeLeft(c.expires_at)}`} />
              )}
              <span className="truncate">{c.title}</span>
            </div>
            {c.incognito ? (
              <div className="conv-preview">Incognito{c.expires_at ? ` · gone in ${timeLeft(c.expires_at)}` : ''}</div>
            ) : (
              preview && <div className="conv-preview">{stripMarkdown(preview)}</div>
            )}
          </button>
          <span className="conv-time">
            <RelativeTime ts={c.updated_at} />
          </span>
          <IconButton
            icon="more"
            label="Conversation menu"
            size="sm"
            className="conv-more"
            aria-haspopup="menu"
            aria-expanded={Boolean(menuAnchor)}
            onClick={(e) => {
              setMenuMode('main');
              setMenuAnchor(menuAnchor ? null : e.currentTarget);
            }}
          />
          {menuAnchor && (
            <Menu
              anchor={menuAnchor}
              onClose={() => setMenuAnchor(null)}
              items={menuMode === 'main' ? mainItems : menuMode === 'move' ? moveItems : ttlItems}
            />
          )}
        </>
      )}
    </div>
  );
}
