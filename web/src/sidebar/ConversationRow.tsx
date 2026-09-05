import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { IconButton, InlineConfirm, Menu, RelativeTime } from '../components/primitives';
import { stripMarkdown } from '../lib/format';
import { previewFor } from '../lib/injected';
import type { Conversation } from '../protocol/types';
import { useStore } from '../store/store';

interface Props {
  conversation: Conversation;
  active: boolean;
}

export function ConversationRow({ conversation: c, active }: Props) {
  const open = useStore((s) => s.openConversation);
  const preview = useStore((s) => previewFor(c, s.messages[c.id]));
  const rename = useStore((s) => s.renameConversation);
  const archive = useStore((s) => s.archiveConversation);
  const remove = useStore((s) => s.deleteConversation);
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
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

  if (confirmDelete) {
    return (
      <div className="conv">
        <InlineConfirm text={`Delete "${c.title}"?`} confirmLabel="Delete" danger onConfirm={() => void remove(c.id)} onCancel={() => setConfirmDelete(false)} />
      </div>
    );
  }

  const archived = c.archived;
  return (
    <div className={`conv${active ? ' active' : ''}${c.unread ? ' unread' : ''}${menuAnchor ? ' menu-open' : ''}`}>
      {editing ? (
        <input ref={inputRef} className="input conv-rename" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={commit} onKeyDown={onKey} aria-label="Conversation title" />
      ) : (
        <>
          <button type="button" className="conv-main" onClick={() => void open(c.id)} aria-current={active ? 'page' : undefined} style={{ textAlign: 'left', color: 'inherit' }}>
            <div className="conv-title">
              {c.unread && <span className="unread-dot" aria-label="Unread" />}
              <span className="truncate">{c.title}</span>
            </div>
            {preview && <div className="conv-preview">{stripMarkdown(preview)}</div>}
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
            onClick={(e) => setMenuAnchor(menuAnchor ? null : e.currentTarget)}
          />
          {menuAnchor && (
            <Menu
              anchor={menuAnchor}
              onClose={() => setMenuAnchor(null)}
              items={[
                { label: 'Rename', icon: 'edit', onSelect: () => setEditing(true) },
                { label: archived ? 'Unarchive' : 'Archive', icon: archived ? 'unarchive' : 'archive', onSelect: () => void archive(c.id, !archived) },
                { label: 'Delete', icon: 'trash', danger: true, onSelect: () => setConfirmDelete(true) },
              ]}
            />
          )}
        </>
      )}
    </div>
  );
}
