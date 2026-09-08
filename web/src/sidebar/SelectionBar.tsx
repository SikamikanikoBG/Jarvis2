import { useState } from 'react';
import { IconButton, InlineConfirm, Menu, type MenuItem } from '../components/primitives';
import { useStore } from '../store/store';

interface Props {
  /** The rows currently on screen, for "select all". */
  visibleIds: string[];
  /** Ask the sidebar for its inline "name the folder" input, then file the selection into it. */
  onNewFolder: () => void;
  onExit: () => void;
}

/**
 * What to do with the chats that are ticked. One request per gesture
 * (`POST /api/conversations/bulk`), so clearing out thirty dead chats is one round trip and one
 * round of events rather than thirty of each.
 */
export function SelectionBar({ visibleIds, onNewFolder, onExit }: Props) {
  const selection = useStore((s) => s.selection);
  const setSelection = useStore((s) => s.setSelection);
  const clearSelection = useStore((s) => s.clearSelection);
  const bulk = useStore((s) => s.bulkSelected);
  const folders = useStore((s) => s.folders);
  const [confirm, setConfirm] = useState(false);
  const [moveAnchor, setMoveAnchor] = useState<HTMLElement | null>(null);
  const n = selection.length;
  const all = visibleIds.length > 0 && visibleIds.every((id) => selection.includes(id));

  /** Fire the action and leave select mode; `bulkSelected` empties the selection itself. */
  const run = (action: Promise<void>) => {
    void action;
    onExit();
  };

  if (confirm) {
    return (
      <div className="selection-bar">
        <InlineConfirm
          text={`Delete ${n} ${n === 1 ? 'chat' : 'chats'}?`}
          confirmLabel="Delete"
          danger
          onConfirm={() => {
            setConfirm(false);
            run(bulk('delete'));
          }}
          onCancel={() => setConfirm(false)}
        />
      </div>
    );
  }

  const moveItems: MenuItem[] = [
    ...folders.map((f) => ({ label: f.name, icon: 'folder' as const, onSelect: () => run(bulk('move', f.id)) })),
    { label: 'New folder…', icon: 'folderPlus', onSelect: onNewFolder },
    ...(folders.length > 0 ? [{ label: 'Out of every folder', icon: 'x' as const, onSelect: () => run(bulk('move', null)) }] : []),
  ];

  return (
    <div className="selection-bar" role="toolbar" aria-label="Selected chats">
      <span className="selection-count">{n} selected</span>
      <IconButton
        icon={all ? 'square' : 'checkSquare'}
        label={all ? 'Select none' : 'Select all shown'}
        size="sm"
        onClick={() => (all ? clearSelection() : setSelection(visibleIds))}
      />
      <IconButton
        icon="folder"
        label="Move to folder"
        size="sm"
        disabled={n === 0}
        aria-haspopup="menu"
        onClick={(e) => setMoveAnchor(moveAnchor ? null : e.currentTarget)}
      />
      <IconButton icon="archive" label="Archive selected" size="sm" disabled={n === 0} onClick={() => run(bulk('archive'))} />
      <IconButton icon="trash" label="Delete selected" size="sm" className="danger" disabled={n === 0} onClick={() => setConfirm(true)} />
      <IconButton icon="x" label="Done selecting" size="sm" onClick={onExit} />
      {moveAnchor && <Menu anchor={moveAnchor} onClose={() => setMoveAnchor(null)} items={moveItems} />}
    </div>
  );
}
