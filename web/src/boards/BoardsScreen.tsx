import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { Highlight } from '../components/Highlight';
import { IconButton, InlineConfirm, Menu, RelativeTime } from '../components/primitives';
import { ScreenFilter } from '../components/ScreenFilter';
import { matchesQuery } from '../lib/filter';
import { errorText, useLoader } from '../lib/useLoader';
import { NOTE_COLORS, type Board, type Note, type NoteColor } from '../protocol/types';
import { useStore } from '../store/store';

/** Sticky-note boards as columns. Every mutation is echoed by `board.changed`, which refetches. */
export function BoardsScreen() {
  const version = useStore((s) => s.featureVersion.boards);
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.boards.list(), []);
  const { data, error, loading, reload } = useLoader(load, version);
  const all = [...(data ?? [])].sort((a, b) => a.position - b.position);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const [query, setQuery] = useState('');
  /**
   * How many notes each column has matching the query. The notes live in the columns (one
   * request each), so only they can answer this — the parent needs it to drop the columns that
   * hold nothing, and to tell Arsen when none of them hold anything.
   */
  const [hits, setHits] = useState<Record<string, number>>({});
  const q = query.trim();
  const onHits = useCallback((id: string, n: number) => {
    setHits((prev) => (prev[id] === n ? prev : { ...prev, [id]: n }));
  }, []);
  // A board whose NAME matches is itself the hit and keeps all its notes.
  const boards = q ? all.filter((b) => matchesQuery(q, b.name) || (hits[b.id] ?? 0) > 0) : all;
  const noteHits = all.reduce((n, b) => n + (matchesQuery(q, b.name) ? 0 : (hits[b.id] ?? 0)), 0);

  const addBoard = () => {
    const n = name.trim();
    if (!n) return;
    api.boards
      .create(n)
      .then(() => {
        setName('');
        setAdding(false);
        reload();
      })
      .catch((e: unknown) => notify(`Could not add the board: ${errorText(e)}`, 'error'));
  };

  return (
    <div className="screen boards-screen">
      <div className="screen-inner wide">
        <div className="section-head">
          <h1>Boards</h1>
          <div className="row">
            <p>Pinned notes; every board is part of what Jarvis reads.</p>
            {!adding && (
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => setAdding(true)}>
                <Icon name="plus" size={15} />
                Add board
              </button>
            )}
          </div>
        </div>
        {error && <div className="field-error">{error}</div>}
        {adding && (
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              addBoard();
            }}
          >
            <input className="input" style={{ maxWidth: 320 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="Board name" autoFocus aria-label="Board name" />
            <button type="submit" className="btn btn-primary btn-sm" disabled={!name.trim()}>
              Add
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAdding(false)}>
              Cancel
            </button>
          </form>
        )}
        {all.length > 0 && (
          <ScreenFilter query={query} onQuery={setQuery} placeholder="Search boards and notes" shown={boards.length} total={all.length} noun="board" />
        )}
        {q && noteHits > 0 && (
          <p className="small muted" style={{ margin: 0 }}>
            {noteHits} matching {noteHits === 1 ? 'note' : 'notes'}.
          </p>
        )}
        {!loading && all.length === 0 && !adding && (
          <div className="empty">
            <strong>No boards yet</strong>
            <span>Add one, then pin replies from the chat or write notes here.</span>
          </div>
        )}
        {!loading && all.length > 0 && boards.length === 0 && (
          <div className="empty">
            <strong>Nothing matches</strong>
            <span>No board name and no note matches the search.</span>
          </div>
        )}
        <div className="boards-row">
          {boards.map((b) => (
            // `all` for the neighbours and the index: reordering must mean the real position,
            // never a position inside a filtered view.
            <BoardColumn key={b.id} board={b} boards={all} index={all.indexOf(b)} query={q} onHits={onHits} version={version} reloadAll={reload} />
          ))}
        </div>
      </div>
    </div>
  );
}

interface ColumnProps {
  board: Board;
  boards: Board[];
  index: number;
  /** Trimmed filter query; "" shows everything. */
  query: string;
  /** Report how many of this column's notes match, so the parent can hide an empty column. */
  onHits: (boardId: string, n: number) => void;
  version: number;
  reloadAll: () => void;
}

function BoardColumn({ board, boards, index, query, onHits, version, reloadAll }: ColumnProps) {
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.boards.notes(board.id), [board.id]);
  const { data: notes, reload } = useLoader(load, `${version}:${board.updated_at}`);
  const [menu, setMenu] = useState<HTMLElement | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(board.name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [draft, setDraft] = useState('');
  const [draftColor, setDraftColor] = useState<NoteColor>('yellow');
  const renameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renaming) renameRef.current?.select();
  }, [renaming]);

  const nameMatches = matchesQuery(query, board.name);
  // A board found by its own name keeps all of its notes; otherwise only the notes that matched.
  const shown = !query || nameMatches ? (notes ?? []) : (notes ?? []).filter((n) => matchesQuery(query, n.text));
  const matching = (notes ?? []).filter((n) => matchesQuery(query, n.text)).length;
  useEffect(() => {
    if (notes) onHits(board.id, matching);
  }, [notes, matching, board.id, onHits]);

  const fail = (what: string) => (e: unknown) => notify(`${what} failed: ${errorText(e)}`, 'error');

  const commitRename = () => {
    setRenaming(false);
    const t = title.trim();
    if (t && t !== board.name) api.boards.patch(board.id, { name: t }).then(reloadAll).catch(fail('Rename'));
    else setTitle(board.name);
  };
  const move = (delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= boards.length) return;
    api.boards.patch(board.id, { position: target }).then(reloadAll).catch(fail('Reorder'));
  };
  const remove = () => api.boards.remove(board.id).then(reloadAll).catch(fail('Delete'));
  const addNote = () => {
    const text = draft.trim();
    if (!text) return;
    api.boards
      .addNote(board.id, { text, color: draftColor })
      .then(() => {
        setDraft('');
        reload();
        reloadAll();
      })
      .catch(fail('Add note'));
  };
  const onDraftKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      addNote();
    }
  };

  return (
    <section className="board-col" aria-label={board.name}>
      <header className="board-head">
        {renaming ? (
          <input
            ref={renameRef}
            className="input conv-rename"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitRename();
              if (e.key === 'Escape') {
                setTitle(board.name);
                setRenaming(false);
              }
            }}
            aria-label="Board name"
          />
        ) : (
          <>
            <h2 className="truncate" onDoubleClick={() => setRenaming(true)}>
              <Highlight text={board.name} query={query} />
            </h2>
            <span className="chip chip-outline">
              {notes && shown.length !== notes.length ? `${shown.length}/${notes.length}` : (notes?.length ?? board.note_count)}
            </span>
            <IconButton icon="more" label={`${board.name} menu`} size="sm" aria-haspopup="menu" aria-expanded={Boolean(menu)} onClick={(e) => setMenu(menu ? null : e.currentTarget)} />
            {menu && (
              <Menu
                anchor={menu}
                onClose={() => setMenu(null)}
                items={[
                  { label: 'Rename', icon: 'edit', onSelect: () => setRenaming(true) },
                  ...(index > 0 ? [{ label: 'Move left', icon: 'chevronLeft' as const, onSelect: () => move(-1) }] : []),
                  ...(index < boards.length - 1 ? [{ label: 'Move right', icon: 'chevronRight' as const, onSelect: () => move(1) }] : []),
                  { label: 'Delete board', icon: 'trash', danger: true, onSelect: () => setConfirmDelete(true) },
                ]}
              />
            )}
          </>
        )}
      </header>
      {confirmDelete && (
        <div className="card" style={{ padding: 4 }}>
          <InlineConfirm text={`Delete "${board.name}" and its notes?`} confirmLabel="Delete" danger onConfirm={() => void remove()} onCancel={() => setConfirmDelete(false)} />
        </div>
      )}
      <div className="board-notes">
        {notes?.length === 0 && <div className="empty small" style={{ padding: '14px 8px' }}>Nothing pinned here yet.</div>}
        {notes && notes.length > 0 && shown.length === 0 && <div className="filter-none">No note here matches.</div>}
        {shown.map((n) => (
          <NoteCard key={n.id} note={n} boards={boards} query={query} onChanged={() => (reload(), reloadAll())} />
        ))}
      </div>
      <div className="note-composer">
        <textarea className="textarea" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onDraftKey} placeholder="New note (Ctrl+Enter to add)" aria-label={`New note on ${board.name}`} />
        <div className="row">
          <ColorPicker value={draftColor} onChange={setDraftColor} />
          <span className="grow" />
          <button type="button" className="btn btn-primary btn-sm" onClick={addNote} disabled={!draft.trim()}>
            Add
          </button>
        </div>
      </div>
    </section>
  );
}

function ColorPicker({ value, onChange }: { value: NoteColor; onChange: (c: NoteColor) => void }) {
  return (
    <div className="swatches" role="radiogroup" aria-label="Colour">
      {NOTE_COLORS.map((c) => (
        <button key={c} type="button" role="radio" aria-checked={value === c} aria-label={c} className={`swatch note-${c}${value === c ? ' sel' : ''}`} onClick={() => onChange(c)} />
      ))}
    </div>
  );
}

function NoteCard({ note, boards, query, onChanged }: { note: Note; boards: Board[]; query: string; onChanged: () => void }) {
  const notify = useStore((s) => s.notify);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(note.text);
  const [tools, setTools] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const fail = (what: string) => (e: unknown) => notify(`${what} failed: ${errorText(e)}`, 'error');

  const save = () => {
    setEditing(false);
    const t = text.trim();
    if (t && t !== note.text) api.notes.patch(note.id, { text: t }).then(onChanged).catch(fail('Edit'));
    else setText(note.text);
  };

  return (
    <article className={`note-card note-${note.color}`}>
      {editing ? (
        <textarea className="note-edit" value={text} onChange={(e) => setText(e.target.value)} onBlur={save} autoFocus rows={Math.min(10, Math.max(3, text.split('\n').length + 1))} aria-label="Note text" />
      ) : (
        <button type="button" className="note-text" onClick={() => setEditing(true)} title="Click to edit">
          <Highlight text={note.text} query={query} />
        </button>
      )}
      <footer className="note-foot">
        <span className="note-when">
          {note.from_message_id && <Icon name="pin" size={11} />}
          <RelativeTime ts={note.updated_at} />
        </span>
        <span className="grow" />
        <IconButton icon="palette" label="Colour and board" size="sm" active={tools} onClick={() => setTools(!tools)} />
        <IconButton icon="trash" label="Delete note" size="sm" onClick={() => setConfirmDelete(true)} />
      </footer>
      {tools && (
        <div className="note-tools">
          <ColorPicker value={note.color} onChange={(c) => void api.notes.patch(note.id, { color: c }).then(onChanged).catch(fail('Recolour'))} />
          {boards.length > 1 && (
            <select className="select" value={note.board_id} onChange={(e) => void api.notes.patch(note.id, { board_id: e.target.value }).then(onChanged).catch(fail('Move'))} aria-label="Move to board">
              {boards.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
      {confirmDelete && <InlineConfirm text="Delete this note?" confirmLabel="Delete" danger onConfirm={() => void api.notes.remove(note.id).then(onChanged).catch(fail('Delete'))} onCancel={() => setConfirmDelete(false)} />}
    </article>
  );
}
