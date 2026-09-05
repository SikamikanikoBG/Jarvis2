"""Notes boards: sticky notes Arsen pins; every board is fed back into the model's context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Board, Note, ToolResult, new_id
from jarvis_proto.events import BoardChanged
from jarvis_proto.features import NoteColor


def _now() -> str:
    return datetime.now(UTC).isoformat()


class BoardStore:
    def __init__(self, db: Database, bus: EventBus | None = None) -> None:
        self.db = db
        self.bus = bus

    def _changed(self, board_id: str | None) -> None:
        if self.bus is not None:
            self.bus.publish(BoardChanged(board_id=board_id))

    # --- boards -----------------------------------------------------------------------

    async def list_boards(self) -> list[Board]:
        rows = await self.db.fetchall(
            "SELECT b.*, (SELECT COUNT(*) FROM notes n WHERE n.board_id = b.id) AS note_count"
            " FROM boards b ORDER BY b.position, b.created_at"
        )
        return [Board(**dict(r)) for r in rows]

    async def get_board(self, board_id: str) -> Board | None:
        row = await self.db.fetchone(
            "SELECT b.*, (SELECT COUNT(*) FROM notes n WHERE n.board_id = b.id) AS note_count"
            " FROM boards b WHERE b.id = ?",
            (board_id,),
        )
        return Board(**dict(row)) if row else None

    async def find_board(self, name_or_id: str) -> Board | None:
        board = await self.get_board(name_or_id)
        if board is not None:
            return board
        row = await self.db.fetchone("SELECT id FROM boards WHERE name = ? COLLATE NOCASE", (name_or_id.strip(),))
        return await self.get_board(row["id"]) if row else None

    async def create_board(self, name: str) -> Board:
        row = await self.db.fetchone("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM boards")
        board = Board(id=new_id("brd"), name=name.strip(), position=int(row["p"]) if row else 0)
        await self.db.execute(
            "INSERT INTO boards(id, name, position, created_at, updated_at) VALUES (?,?,?,?,?)",
            (board.id, board.name, board.position, board.created_at.isoformat(), board.updated_at.isoformat()),
        )
        self._changed(None)
        return board

    async def update_board(
        self, board_id: str, *, name: str | None = None, position: int | None = None
    ) -> Board | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name.strip())
        if position is not None:
            sets.append("position = ?")
            params.append(position)
        if sets:
            sets.append("updated_at = ?")
            params.extend([_now(), board_id])
            await self.db.execute(f"UPDATE boards SET {', '.join(sets)} WHERE id = ?", params)
            self._changed(board_id)
        return await self.get_board(board_id)

    async def reorder_board(self, board_id: str, position: int) -> Board | None:
        """Move a board to a target index and renumber the rest (what the UI sends)."""
        boards = await self.list_boards()
        ids = [b.id for b in boards]
        if board_id not in ids:
            return None
        ids.remove(board_id)
        ids.insert(max(0, min(position, len(ids))), board_id)
        now = _now()
        for idx, bid in enumerate(ids):
            await self.db.execute("UPDATE boards SET position = ?, updated_at = ? WHERE id = ?", (idx, now, bid))
        self._changed(None)
        return await self.get_board(board_id)

    async def delete_board(self, board_id: str) -> None:
        await self.db.execute("DELETE FROM boards WHERE id = ?", (board_id,))
        self._changed(None)

    # --- notes ------------------------------------------------------------------------

    async def list_notes(self, board_id: str) -> list[Note]:
        rows = await self.db.fetchall(
            "SELECT * FROM notes WHERE board_id = ? ORDER BY position, created_at", (board_id,)
        )
        return [Note(**dict(r)) for r in rows]

    async def get_note(self, note_id: str) -> Note | None:
        row = await self.db.fetchone("SELECT * FROM notes WHERE id = ?", (note_id,))
        return Note(**dict(row)) if row else None

    async def add_note(
        self, board_id: str, text: str, *, color: NoteColor = "yellow", from_message_id: str | None = None
    ) -> Note:
        row = await self.db.fetchone(
            "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM notes WHERE board_id = ?", (board_id,)
        )
        note = Note(
            id=new_id("note"),
            board_id=board_id,
            text=text.strip(),
            color=color,
            from_message_id=from_message_id,
            position=int(row["p"]) if row else 0,
        )
        await self.db.execute(
            "INSERT INTO notes(id, board_id, text, color, from_message_id, position, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                note.id,
                note.board_id,
                note.text,
                note.color,
                note.from_message_id,
                note.position,
                note.created_at.isoformat(),
                note.updated_at.isoformat(),
            ),
        )
        await self.db.execute("UPDATE boards SET updated_at = ? WHERE id = ?", (_now(), board_id))
        self._changed(board_id)
        return note

    async def update_note(self, note_id: str, **fields: Any) -> Note | None:
        allowed = {"text", "color", "board_id", "position"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            sets.append(f"{key} = ?")
            params.append(value.strip() if key == "text" else value)
        note = await self.get_note(note_id)
        if note is None:
            return None
        if sets:
            sets.append("updated_at = ?")
            params.extend([_now(), note_id])
            await self.db.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", params)
            self._changed(note.board_id)
            if fields.get("board_id") and fields["board_id"] != note.board_id:
                self._changed(fields["board_id"])
        return await self.get_note(note_id)

    async def delete_note(self, note_id: str) -> None:
        note = await self.get_note(note_id)
        await self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self._changed(note.board_id if note else None)

    # --- context ----------------------------------------------------------------------

    async def context_block(self, max_chars: int) -> str | None:
        boards = await self.list_boards()
        if not boards:
            return None
        parts: list[str] = ["## Notes boards (Arsen's pinned notes — treat as current facts)"]
        for board in boards:
            notes = await self.list_notes(board.id)
            if not notes:
                continue
            parts.append(f"### {board.name}")
            parts.extend(f"- {n.text}" for n in notes)
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[: max_chars - 40].rstrip() + "\n… (boards truncated)"
        return text if len(parts) > 1 else None


# --- tools ------------------------------------------------------------------------------


class _ListArgs(BaseModel):
    board: str = Field(description="Board name or id")


class _AddArgs(BaseModel):
    board: str = Field(description="Board name or id; created if it does not exist")
    text: str = Field(description="The note text, one fact or task")
    color: NoteColor = "yellow"


class _UpdateArgs(BaseModel):
    note_id: str
    text: str | None = None
    color: NoteColor | None = None


class _RemoveArgs(BaseModel):
    note_id: str


class NotesTools(BuiltinProvider):
    name = "notes"

    def __init__(self, store: BoardStore) -> None:
        self.store = store
        super().__init__()

    @tool("notes.boards", description="List Arsen's notes boards with note counts.", read_only=True, idempotent=True)
    async def _boards(self) -> ToolResult:
        boards = await self.store.list_boards()
        if not boards:
            return ToolResult.empty("No boards yet.")
        return ToolResult.data(
            "\n".join(f"- {b.name} ({b.note_count} notes, id {b.id})" for b in boards), count=len(boards)
        )

    @tool("notes.list", description="List the notes on one board.", args=_ListArgs, read_only=True, idempotent=True)
    async def _list(self, board: str) -> ToolResult:
        b = await self.store.find_board(board)
        if b is None:
            return ToolResult.failure(f"no board named {board!r}")
        notes = await self.store.list_notes(b.id)
        if not notes:
            return ToolResult.empty(f"Board {b.name!r} is empty.")
        return ToolResult.data("\n".join(f"- [{n.id}] {n.text}" for n in notes), count=len(notes))

    @tool(
        "notes.add", description="Pin a note to a board (creates the board if needed).", args=_AddArgs, idempotent=False
    )
    async def _add(self, board: str, text: str, color: NoteColor = "yellow") -> ToolResult:
        b = await self.store.find_board(board) or await self.store.create_board(board)
        note = await self.store.add_note(b.id, text, color=color)
        return ToolResult.data(f"Pinned to {b.name!r} as {note.id}.")

    @tool("notes.update", description="Change a note's text or color.", args=_UpdateArgs)
    async def _update(self, note_id: str, text: str | None = None, color: NoteColor | None = None) -> ToolResult:
        note = await self.store.update_note(note_id, text=text, color=color)
        return ToolResult.data(f"Updated {note_id}.") if note else ToolResult.failure(f"no note {note_id!r}")

    @tool("notes.remove", description="Remove a note.", args=_RemoveArgs)
    async def _remove(self, note_id: str) -> ToolResult:
        if await self.store.get_note(note_id) is None:
            return ToolResult.failure(f"no note {note_id!r}")
        await self.store.delete_note(note_id)
        return ToolResult.data(f"Removed {note_id}.")
