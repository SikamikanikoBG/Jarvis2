import { useState } from 'react';
import { api } from '../api/client';
import { IconButton, Menu, type MenuItem } from '../components/primitives';
import { errorText } from '../lib/useLoader';
import type { Board } from '../protocol/types';
import { useStore } from '../store/store';

interface Props {
  messageId: string;
  text: string;
}

/** "Pin" an assistant reply as a sticky note: picks a board, POSTs the note with from_message_id. */
export function PinButton({ messageId, text }: Props) {
  const notify = useStore((s) => s.notify);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [boards, setBoards] = useState<Board[] | null>(null);
  const [busy, setBusy] = useState(false);

  const open = (el: HTMLElement) => {
    setAnchor(el);
    api.boards
      .list()
      .then(setBoards)
      .catch((e: unknown) => {
        setAnchor(null);
        notify(`Could not load boards: ${errorText(e)}`, 'error');
      });
  };

  const pin = (board: Board) => {
    setBusy(true);
    api.boards
      .addNote(board.id, { text, from_message_id: messageId })
      .then(() => notify(`Pinned to ${board.name}.`))
      .catch((e: unknown) => notify(`Pin failed: ${errorText(e)}`, 'error'))
      .finally(() => setBusy(false));
  };

  const items: MenuItem[] =
    boards === null
      ? [{ label: 'Loading boards…', onSelect: () => undefined }]
      : boards.length === 0
        ? [{ label: 'No boards yet — create one in Boards', onSelect: () => undefined }]
        : boards.map((b) => ({ label: b.name, icon: 'pin' as const, onSelect: () => pin(b) }));

  return (
    <>
      <IconButton icon="pin" label="Pin to a board" size="sm" className="msg-action" disabled={busy} aria-haspopup="menu" aria-expanded={Boolean(anchor)} onClick={(e) => (anchor ? setAnchor(null) : open(e.currentTarget))} />
      {anchor && <Menu anchor={anchor} onClose={() => setAnchor(null)} items={items} />}
    </>
  );
}
