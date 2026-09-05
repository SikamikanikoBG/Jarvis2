"""Typed access to the tables. One class, sections per table; no ORM."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from jarvis_core.db.connection import Database
from jarvis_proto import (
    Conversation,
    ConversationKind,
    Message,
    ModelUsage,
    Plan,
    Role,
    Run,
    RunBudget,
    RunKind,
    RunStatus,
    Settings,
    ToolCall,
    new_id,
)
from jarvis_proto.events import RunEvent


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- conversations --------------------------------------------------------------

    @staticmethod
    def _conversation(row: aiosqlite.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            kind=ConversationKind(row["kind"]),
            title=row["title"],
            folder_key=row["folder_key"],
            folder_label=row["folder_label"],
            archived=bool(row["archived"]),
            unread=bool(row["unread"]),
            preview=row["preview"],
            message_count=row["message_count"],
            created_at=_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=_dt(row["updated_at"]) or datetime.now(UTC),
        )

    async def create_conversation(
        self,
        *,
        kind: ConversationKind = ConversationKind.CHAT,
        title: str = "New chat",
        folder_key: str | None = None,
        folder_label: str | None = None,
    ) -> Conversation:
        conv = Conversation(id=new_id("conv"), kind=kind, title=title, folder_key=folder_key, folder_label=folder_label)
        await self.db.execute(
            "INSERT INTO conversations(id, kind, title, folder_key, folder_label, archived, unread,"
            " preview, message_count, created_at, updated_at) VALUES (?,?,?,?,?,0,0,NULL,0,?,?)",
            (
                conv.id,
                conv.kind.value,
                conv.title,
                conv.folder_key,
                conv.folder_label,
                conv.created_at.isoformat(),
                conv.updated_at.isoformat(),
            ),
        )
        return conv

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return self._conversation(row) if row else None

    async def list_conversations(self, *, include_archived: bool = False) -> list[Conversation]:
        sql = "SELECT * FROM conversations"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY updated_at DESC"
        return [self._conversation(r) for r in await self.db.fetchall(sql)]

    async def update_conversation(self, conversation_id: str, **fields: Any) -> Conversation | None:
        allowed = {"title", "archived", "unread", "preview", "folder_key", "folder_label", "kind"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update {key}")
            sets.append(f"{key} = ?")
            params.append(value.value if isinstance(value, ConversationKind) else value)
        if not sets:
            return await self.get_conversation(conversation_id)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(conversation_id)
        await self.db.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?", params)
        return await self.get_conversation(conversation_id)

    async def touch_conversation(self, conversation_id: str, preview: str | None) -> None:
        await self.db.execute(
            "UPDATE conversations SET updated_at = ?, preview = COALESCE(?, preview),"
            " message_count = (SELECT COUNT(*) FROM messages WHERE conversation_id = ?)"
            " WHERE id = ?",
            (_now(), (preview or "")[:160] or None, conversation_id, conversation_id),
        )

    async def delete_conversation(self, conversation_id: str) -> None:
        await self.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    # --- messages -------------------------------------------------------------------

    @staticmethod
    def _message(row: aiosqlite.Row) -> Message:
        raw_calls = json.loads(row["tool_calls"]) if row["tool_calls"] else []
        return Message(
            id=row["id"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            role=Role(row["role"]),
            content=row["content"],
            reasoning=row["reasoning"],
            tool_calls=[ToolCall.model_validate(c) for c in raw_calls],
            tool_call_id=row["tool_call_id"],
            name=row["name"],
            partial=bool(row["partial"]),
            created_at=_dt(row["created_at"]) or datetime.now(UTC),
        )

    async def add_message(self, message: Message) -> Message:
        if message.id is None:
            message.id = new_id("msg")
        if message.conversation_id is None:
            raise ValueError("message needs a conversation_id")
        await self.db.execute(
            "INSERT INTO messages(id, conversation_id, run_id, role, content, reasoning, tool_calls,"
            " tool_call_id, name, partial, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                message.id,
                message.conversation_id,
                message.run_id,
                message.role.value,
                message.content,
                message.reasoning,
                json.dumps([c.model_dump() for c in message.tool_calls]) if message.tool_calls else None,
                message.tool_call_id,
                message.name,
                int(message.partial),
                message.created_at.isoformat(),
            ),
        )
        preview = message.content if message.role in {Role.USER, Role.ASSISTANT} else None
        await self.touch_conversation(message.conversation_id, preview)
        return message

    async def list_messages(self, conversation_id: str, *, limit: int | None = None) -> list[Message]:
        sql = "SELECT * FROM messages WHERE conversation_id = ? ORDER BY rowid"
        rows = await self.db.fetchall(sql, (conversation_id,))
        if limit is not None:
            rows = rows[-limit:]
        return [self._message(r) for r in rows]

    async def list_run_messages(self, run_id: str) -> list[Message]:
        rows = await self.db.fetchall("SELECT * FROM messages WHERE run_id = ? ORDER BY rowid", (run_id,))
        return [self._message(r) for r in rows]

    # --- runs -----------------------------------------------------------------------

    @staticmethod
    def _run(row: aiosqlite.Row) -> Run:
        return Run(
            id=row["id"],
            conversation_id=row["conversation_id"],
            kind=RunKind(row["kind"]),
            status=RunStatus(row["status"]),
            input_text=row["input_text"],
            plan=Plan.model_validate_json(row["plan"]) if row["plan"] else None,
            budget=RunBudget.model_validate_json(row["budget"]),
            priority=row["priority"],
            steps_used=row["steps_used"],
            usage=ModelUsage.model_validate_json(row["usage"]),
            last_seq=row["last_seq"],
            error=row["error"],
            waiting_reason=row["waiting_reason"],
            created_at=_dt(row["created_at"]) or datetime.now(UTC),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
        )

    async def create_run(self, run: Run) -> Run:
        await self.db.execute(
            "INSERT INTO runs(id, conversation_id, kind, status, input_text, plan, budget, priority,"
            " steps_used, usage, last_seq, error, waiting_reason, created_at, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.id,
                run.conversation_id,
                run.kind.value,
                run.status.value,
                run.input_text,
                run.plan.model_dump_json() if run.plan else None,
                run.budget.model_dump_json(),
                run.priority,
                run.steps_used,
                run.usage.model_dump_json(),
                run.last_seq,
                run.error,
                run.waiting_reason,
                run.created_at.isoformat(),
                run.started_at.isoformat() if run.started_at else None,
                run.finished_at.isoformat() if run.finished_at else None,
            ),
        )
        return run

    async def save_run(self, run: Run) -> None:
        await self.db.execute(
            "UPDATE runs SET status=?, plan=?, steps_used=?, usage=?, last_seq=?, error=?,"
            " waiting_reason=?, started_at=?, finished_at=? WHERE id=?",
            (
                run.status.value,
                run.plan.model_dump_json() if run.plan else None,
                run.steps_used,
                run.usage.model_dump_json(),
                run.last_seq,
                run.error,
                run.waiting_reason,
                run.started_at.isoformat() if run.started_at else None,
                run.finished_at.isoformat() if run.finished_at else None,
                run.id,
            ),
        )

    async def get_run(self, run_id: str) -> Run | None:
        row = await self.db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run(row) if row else None

    async def list_runs(self, conversation_id: str, *, limit: int = 50) -> list[Run]:
        rows = await self.db.fetchall(
            "SELECT * FROM runs WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
            (conversation_id, limit),
        )
        return [self._run(r) for r in rows]

    async def runs_with_status(self, *statuses: RunStatus) -> list[Run]:
        marks = ",".join("?" for _ in statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY priority, created_at",
            [s.value for s in statuses],
        )
        return [self._run(r) for r in rows]

    # --- run events -----------------------------------------------------------------

    async def append_event(self, event: RunEvent) -> None:
        await self.db.execute(
            "INSERT INTO run_events(run_id, seq, type, payload, ts) VALUES (?,?,?,?,?)",
            (
                event.run_id,
                event.seq,
                getattr(event, "type", ""),
                event.model_dump_json(),
                event.ts.isoformat(),
            ),
        )
        # last_seq must never lag the events table, or a resume after a crash collides.
        await self.db.execute("UPDATE runs SET last_seq = MAX(last_seq, ?) WHERE id = ?", (event.seq, event.run_id))

    async def list_events(self, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT payload FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        )
        return [json.loads(r["payload"]) for r in rows]

    # --- settings -------------------------------------------------------------------

    async def load_settings(self) -> Settings:
        rows = await self.db.fetchall("SELECT key, value FROM settings")
        data = {r["key"]: json.loads(r["value"]) for r in rows}
        return Settings.model_validate(data)

    async def save_settings(self, settings: Settings, *, only_keys: set[str] | None = None) -> None:
        dump = settings.model_dump(mode="json")
        now = _now()
        rows = [(key, json.dumps(value), now) for key, value in dump.items() if only_keys is None or key in only_keys]
        await self.db.executemany(
            "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            rows,
        )

    # --- idempotency ----------------------------------------------------------------

    async def claim_idempotency(self, key: str, run_id: str, tool: str) -> bool:
        """True if the key was free (claimed now); False if it was already used."""
        try:
            await self.db.execute(
                "INSERT INTO idempotency(key, run_id, tool, result, created_at) VALUES (?,?,?,NULL,?)",
                (key, run_id, tool, _now()),
            )
        except Exception:  # sqlite3.IntegrityError, but aiosqlite re-raises the sqlite3 type
            return False
        return True

    async def record_idempotent_result(self, key: str, result_json: str) -> None:
        await self.db.execute("UPDATE idempotency SET result = ? WHERE key = ?", (result_json, key))

    async def idempotent_result(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT result FROM idempotency WHERE key = ?", (key,))
        return row["result"] if row else None
