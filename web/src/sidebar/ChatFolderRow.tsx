import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from 'react';
import { Icon } from '../components/Icon';
import { IconButton, InlineConfirm, Menu } from '../components/primitives';
import type { ChatFolderSection } from '../store/selectors';
import { useStore } from '../store/store';
import { ActivityCount } from './ActivityDot';
import { ConversationRow } from './ConversationRow';
import { isConversationDrag, readDragIds } from './dnd';

interface Props {
  section: ChatFolderSection;
  openId: string | null;
  selecting: boolean;
  ordered: string[];
}

/**
 * One of Arsen's own folders: a head that collapses, counts, takes a drop, and renames or
 * deletes itself; the chats filed in it underneath.
 *
 * Collapse state is remembered per device (`lib/folders.ts`) rather than reset on every load —
 * a folder is filing, and filing you have closed should stay closed.
 */
export function ChatFolderRow({ section, openId, selecting, ordered }: Props) {
  const { folder, conversations } = section;
  const collapsed = useStore((s) => s.collapsedFolders.includes(folder.id));
  const toggleCollapsed = useStore((s) => s.toggleFolderCollapsed);
  const renameFolder = useStore((s) => s.renameFolder);
  const deleteFolder = useStore((s) => s.deleteFolder);
  const move = useStore((s) => s.moveConversation);
  const selection = useStore((s) => s.selection);
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [dropping, setDropping] = useState(false);
  const [name, setName] = useState(folder.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const commit = () => {
    setEditing(false);
    const next = name.trim();
    if (next && next !== folder.name) void renameFolder(folder.id, next);
    else setName(folder.name);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') commit();
    if (e.key === 'Escape') {
      setName(folder.name);
      setEditing(false);
    }
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    // The list itself accepts drops too (that is how a chat leaves every folder), and this
    // folder sits inside it — without stopping the bubble, filing a chat instantly un-filed it.
    e.stopPropagation();
    setDropping(false);
    const ids = readDragIds(e.dataTransfer);
    for (const id of ids) void move(id, folder.id);
    // A drop into a folder is the end of that selection; leaving it ticked reads as pending work.
    if (ids.length > 1 && ids.every((id) => selection.includes(id))) useStore.getState().clearSelection();
    if (ids.length > 0 && collapsed) toggleCollapsed(folder.id); // show where they landed
  };

  if (confirmDelete) {
    return (
      <div className="sidebar-section">
        <InlineConfirm
          text={conversations.length > 0 ? `Delete "${folder.name}"? Its ${conversations.length} chats stay.` : `Delete "${folder.name}"?`}
          confirmLabel="Delete folder"
          danger
          onConfirm={() => void deleteFolder(folder.id)}
          onCancel={() => setConfirmDelete(false)}
        />
      </div>
    );
  }

  return (
    <div
      className={`sidebar-section chat-folder${dropping ? ' dropping' : ''}`}
      onDragOver={(e) => {
        if (!isConversationDrag(e.dataTransfer)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        setDropping(true);
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={onDrop}
    >
      {editing ? (
        <input ref={inputRef} className="input conv-rename" value={name} onChange={(e) => setName(e.target.value)} onBlur={commit} onKeyDown={onKey} aria-label="Folder name" />
      ) : (
        <div className="folder-head-row">
          <button type="button" className="folder-head" aria-expanded={!collapsed} onClick={() => toggleCollapsed(folder.id)}>
            <Icon name="chevronRight" size={14} className="chev" />
            <Icon name="folder" size={14} className="folder-icon" />
            <span className="truncate">{folder.name}</span>
            {section.unread > 0 && <span className="unread-dot" aria-label={`${section.unread} unread`} />}
            <ActivityCount running={section.running} waiting={section.waiting} />
            <span className="count">{conversations.length}</span>
          </button>
          <IconButton
            icon="more"
            label={`${folder.name} folder menu`}
            size="sm"
            className="folder-more"
            aria-haspopup="menu"
            aria-expanded={Boolean(menuAnchor)}
            onClick={(e) => setMenuAnchor(menuAnchor ? null : e.currentTarget)}
          />
          {menuAnchor && (
            <Menu
              anchor={menuAnchor}
              onClose={() => setMenuAnchor(null)}
              items={[
                { label: 'Rename folder', icon: 'edit', onSelect: () => setEditing(true) },
                { label: 'Delete folder', icon: 'trash', danger: true, onSelect: () => setConfirmDelete(true) },
              ]}
            />
          )}
        </div>
      )}
      {!collapsed &&
        (conversations.length === 0 ? (
          <div className="empty small folder-empty">Drag chats here.</div>
        ) : (
          conversations.map((c) => (
            <ConversationRow
              key={c.id}
              conversation={c}
              active={c.id === openId}
              selecting={selecting}
              selected={selection.includes(c.id)}
              ordered={ordered}
            />
          ))
        ))}
    </div>
  );
}
