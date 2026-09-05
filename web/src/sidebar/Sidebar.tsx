import { useEffect, useMemo, useState } from 'react';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { selectSidebar, type Folder } from '../store/selectors';
import { useStore } from '../store/store';
import { ConversationRow } from './ConversationRow';

export function Sidebar() {
  const conversations = useStore((s) => s.conversations);
  const loaded = useStore((s) => s.conversationsLoaded);
  const openId = useStore((s) => s.openConversationId);
  const newChat = useStore((s) => s.newChat);
  const model = useMemo(() => selectSidebar(conversations), [conversations]);

  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="sidebar-head">
        <h2>Chats</h2>
        <IconButton icon="plus" label="New chat" onClick={newChat} />
      </div>
      <div className="sidebar-list">
        {model.chats.length === 0 && loaded && (
          <div className="empty small" style={{ padding: '18px 8px' }}>
            {model.folders.length === 0 ? 'No conversations yet. Start one below.' : 'No chats yet.'}
          </div>
        )}
        {model.chats.map((c) => (
          <ConversationRow key={c.id} conversation={c} active={c.id === openId} />
        ))}
        {model.folders.map((f) => (
          <FolderSection key={f.kind} folder={f} openId={openId} />
        ))}
      </div>
    </nav>
  );
}

function FolderSection({ folder, openId }: { folder: Folder; openId: string | null }) {
  const containsOpen = folder.groups.some((g) => g.conversations.some((c) => c.id === openId));
  // Collapsed by default; a folder holding the open conversation expands on its own until toggled.
  const [manual, setManual] = useState<boolean | null>(null);
  const expanded = manual ?? containsOpen;
  const loadArchived = useStore((s) => s.loadArchived);
  useEffect(() => {
    if (expanded && folder.kind === 'archive') void loadArchived();
  }, [expanded, folder.kind, loadArchived]);

  return (
    <div className="sidebar-section">
      <button type="button" className="folder-head" aria-expanded={expanded} onClick={() => setManual(!expanded)}>
        <Icon name="chevronRight" size={14} className="chev" />
        <span>{folder.label}</span>
        {folder.unread > 0 && <span className="unread-dot" aria-label={`${folder.unread} unread`} />}
        <span className="count">{folder.count}</span>
      </button>
      {expanded &&
        folder.groups.map((g) =>
          g.key === '' ? (
            g.conversations.map((c) => <ConversationRow key={c.id} conversation={c} active={c.id === openId} />)
          ) : (
            <div key={g.key} className="folder-group">
              <div className="folder-group-label truncate">{g.label}</div>
              {g.conversations.map((c) => (
                <ConversationRow key={c.id} conversation={c} active={c.id === openId} />
              ))}
            </div>
          ),
        )}
    </div>
  );
}
