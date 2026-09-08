import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import type { Conversation } from '../protocol/types';
import { selectSidebar, type Folder, type SidebarModel } from '../store/selectors';
import { useStore } from '../store/store';
import { ActivityCount } from './ActivityDot';
import { ChatFolderRow } from './ChatFolderRow';
import { ConversationRow } from './ConversationRow';
import { isConversationDrag, readDragIds } from './dnd';
import { SearchBox, SearchResults } from './SearchBox';
import { SelectionBar } from './SelectionBar';

export function Sidebar() {
  const conversations = useStore((s) => s.conversations);
  const chatFolders = useStore((s) => s.folders);
  const loaded = useStore((s) => s.conversationsLoaded);
  const openId = useStore((s) => s.openConversationId);
  const newChat = useStore((s) => s.newChat);
  const collapsed = useStore((s) => s.collapsedFolders);
  const selection = useStore((s) => s.selection);
  const clearSelection = useStore((s) => s.clearSelection);
  const createFolder = useStore((s) => s.createFolder);
  const bulk = useStore((s) => s.bulkSelected);
  const move = useStore((s) => s.moveConversation);
  const runningOnly = useStore((s) => s.runningOnly);
  const setRunningOnly = useStore((s) => s.setRunningOnly);
  const model = useMemo(
    () => selectSidebar(conversations, chatFolders, { runningOnly }),
    [conversations, chatFolders, runningOnly],
  );
  const [query, setQuery] = useState('');
  // Select mode is entered by the button and left by Done; a ctrl-click on a row also puts a
  // selection on screen without it, which is why "selecting" is either of the two.
  const [selectMode, setSelectMode] = useState(false);
  // `null` = not naming a folder. "selection" = the chats ticked right now go into it once made.
  const [naming, setNaming] = useState<null | 'idle' | 'selection'>(null);
  const [name, setName] = useState('');
  const nameRef = useRef<HTMLInputElement>(null);
  const selecting = selectMode || selection.length > 0;

  useEffect(() => {
    if (naming) nameRef.current?.focus();
  }, [naming]);

  /** Rows in the order they are shown, which is what a shift-click range means. */
  const ordered = useMemo(() => {
    const ids = model.chats.map((c) => c.id);
    for (const section of model.chatFolders) {
      if (collapsed.includes(section.folder.id)) continue;
      ids.push(...section.conversations.map((c) => c.id));
    }
    return ids;
  }, [model, collapsed]);

  const exitSelect = () => {
    setSelectMode(false);
    clearSelection();
  };

  const commitName = () => {
    const trimmed = name.trim();
    const mode = naming;
    setNaming(null);
    setName('');
    if (!trimmed || !mode) return;
    void createFolder(trimmed).then((id) => {
      if (id && mode === 'selection' && selection.length > 0) {
        void bulk('move', id);
        setSelectMode(false);
      }
    });
  };

  /**
   * Dropping on the flat list files the chats out of every folder — but only when the flat list
   * MEANS "no folder". While filtering it is just the live chats, and a stray drop there would
   * silently un-file one.
   */
  const onDropToRoot = (e: DragEvent<HTMLDivElement>) => {
    if (runningOnly || !isConversationDrag(e.dataTransfer)) return;
    e.preventDefault();
    for (const id of readDragIds(e.dataTransfer)) void move(id, null);
  };

  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="sidebar-head">
        <h2>Chats</h2>
        <button
          type="button"
          className={`icon-btn sm live-toggle${runningOnly ? ' active' : ''}`}
          // A toggle keeps one name and says its state through aria-pressed. Renaming it to
          // "Show all chats" while on collided with the empty state's own button of that name.
          aria-pressed={runningOnly}
          aria-label="Only what's running"
          title={
            runningOnly
              ? 'Showing only what is running — click for all chats'
              : model.live > 0
                ? `Show only what is running (${model.live})`
                : 'Show only what is running (nothing right now)'
          }
          onClick={() => setRunningOnly(!runningOnly)}
        >
          <Icon name="activity" size={16} />
          {model.live > 0 && <span className="live-badge">{model.live}</span>}
        </button>
        <IconButton icon="folderPlus" label="New folder" size="sm" onClick={() => setNaming('idle')} />
        <IconButton
          icon="checkSquare"
          label={selecting ? 'Done selecting' : 'Select chats'}
          size="sm"
          active={selecting}
          onClick={() => (selecting ? exitSelect() : setSelectMode(true))}
        />
        <IconButton icon="plus" label="New chat" onClick={newChat} />
      </div>
      <SearchBox query={query} onQuery={setQuery} />
      {naming && (
        <div className="folder-new">
          <Icon name="folder" size={14} />
          <input
            ref={nameRef}
            className="input"
            value={name}
            placeholder="Folder name"
            aria-label="New folder name"
            onChange={(e) => setName(e.target.value)}
            onBlur={commitName}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitName();
              if (e.key === 'Escape') {
                setNaming(null);
                setName('');
              }
            }}
          />
        </div>
      )}
      {selecting && !query.trim() && (
        <SelectionBar visibleIds={ordered} onNewFolder={() => setNaming('selection')} onExit={exitSelect} />
      )}
      {query.trim() ? (
        <SearchResults query={query} />
      ) : (
        <div
          className="sidebar-list"
          onDragOver={(e) => !runningOnly && isConversationDrag(e.dataTransfer) && e.preventDefault()}
          onDrop={onDropToRoot}
        >
          {loaded && <SidebarEmpty model={model} runningOnly={runningOnly} onShowAll={() => setRunningOnly(false)} />}
          {model.chats.map((c) => (
            <ConversationRow
              key={c.id}
              conversation={c}
              active={c.id === openId}
              selecting={selecting}
              selected={selection.includes(c.id)}
              ordered={ordered}
            />
          ))}
          {model.chatFolders.map((section) => (
            <ChatFolderRow key={section.folder.id} section={section} openId={openId} selecting={selecting} ordered={ordered} />
          ))}
          {model.folders.map((f) => (
            <FolderSection key={f.kind} folder={f} openId={openId} selecting={selecting} ordered={ordered} />
          ))}
        </div>
      )}
    </nav>
  );
}

/** What the list says when it has nothing to show — which the filter makes a normal state. */
function SidebarEmpty({ model, runningOnly, onShowAll }: { model: SidebarModel; runningOnly: boolean; onShowAll: () => void }) {
  if (runningOnly) {
    if (model.chats.length > 0) return null;
    return (
      <div className="empty small sidebar-empty">
        Nothing is running.
        <button type="button" className="btn btn-sm btn-ghost" onClick={onShowAll}>
          Show all chats
        </button>
      </div>
    );
  }
  // Filed chats count: with everything tucked into folders, "no chats yet" would be a lie.
  if (model.chats.length > 0 || model.chatFolders.some((s) => s.conversations.length > 0)) return null;
  const empty = model.chatFolders.length === 0 && model.folders.length === 0;
  return <div className="empty small sidebar-empty">{empty ? 'No conversations yet. Start one below.' : 'No chats yet.'}</div>;
}

function FolderSection({
  folder,
  openId,
  selecting,
  ordered,
}: {
  folder: Folder;
  openId: string | null;
  selecting: boolean;
  ordered: string[];
}) {
  const containsOpen = folder.groups.some((g) => g.conversations.some((c) => c.id === openId));
  // Collapsed by default; a folder holding the open conversation expands on its own until toggled.
  const [manual, setManual] = useState<boolean | null>(null);
  const expanded = manual ?? containsOpen;
  const loadArchived = useStore((s) => s.loadArchived);
  const selection = useStore((s) => s.selection);
  const live = (want: string) => folder.groups.reduce((n, g) => n + g.conversations.filter((c) => c.activity === want).length, 0);
  useEffect(() => {
    if (expanded && folder.kind === 'archive') void loadArchived();
  }, [expanded, folder.kind, loadArchived]);

  const row = (c: Conversation) => (
    <ConversationRow
      key={c.id}
      conversation={c}
      active={c.id === openId}
      selecting={selecting}
      selected={selection.includes(c.id)}
      ordered={ordered}
    />
  );

  return (
    <div className="sidebar-section">
      <button type="button" className="folder-head" aria-expanded={expanded} onClick={() => setManual(!expanded)}>
        <Icon name="chevronRight" size={14} className="chev" />
        <span>{folder.label}</span>
        {folder.unread > 0 && <span className="unread-dot" aria-label={`${folder.unread} unread`} />}
        <ActivityCount running={live('running')} waiting={live('waiting')} />
        <span className="count">{folder.count}</span>
      </button>
      {expanded &&
        folder.groups.map((g) =>
          g.key === '' ? (
            g.conversations.map(row)
          ) : (
            <div key={g.key} className="folder-group">
              <div className="folder-group-label truncate">{g.label}</div>
              {g.conversations.map(row)}
            </div>
          ),
        )}
    </div>
  );
}
