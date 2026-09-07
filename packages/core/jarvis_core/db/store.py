"""Typed access to the tables. One class, sections per table; no ORM."""

from __future__ import annotations

import json
import logging
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
    ToolSpec,
    new_id,
)
from jarvis_proto.events import RunEvent
from jarvis_proto.runs import SearchHit

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _escape_like(pattern: str) -> str:
    """Escape LIKE metacharacters inside the user's text; the outer %…% stay live."""
    inner = pattern[1:-1] if pattern.startswith("%") and pattern.endswith("%") else pattern
    inner = inner.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{inner}%"


def _snippet(text: str, query: str, width: int = 70) -> str:
    flat = " ".join(text.split())
    at = flat.lower().find(query.lower())
    if at < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, at - width // 2)
    end = min(len(flat), at + len(query) + width // 2)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


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
            pinned=bool(row["pinned"]),
            title_auto=bool(row["title_auto"]),
            instructions=row["instructions"] or "",
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
        allowed = {
            "title", "archived", "unread", "pinned", "title_auto", "instructions",
            "preview", "folder_key", "folder_label", "kind",
        }
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

    async def search(self, query: str, *, limit: int = 30) -> list[SearchHit]:
        """Titles first, then message text (user/assistant, unnamed = not injected context).
        One hit per conversation; the message hit carries a snippet around the first match."""
        q = " ".join(query.split())
        if not q:
            return []
        like = f"%{q}%"
        hits: list[SearchHit] = []
        seen: set[str] = set()
        rows = await self.db.fetchall(
            "SELECT * FROM conversations WHERE title LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT ?",
            (_escape_like(like), limit),
        )
        for r in rows:
            conv = self._conversation(r)
            seen.add(conv.id)
            hits.append(SearchHit(conversation=conv, matched="title"))
        if len(hits) >= limit:
            return hits
        rows = await self.db.fetchall(
            "SELECT m.id AS message_id, m.content AS content, c.* FROM messages m"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE m.role IN ('user', 'assistant') AND m.name IS NULL AND m.content LIKE ? ESCAPE '\\'"
            " ORDER BY m.rowid DESC LIMIT ?",
            (_escape_like(like), limit * 4),
        )
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            hits.append(
                SearchHit(
                    conversation=self._conversation(r),
                    message_id=r["message_id"],
                    snippet=_snippet(r["content"] or "", q),
                    matched="message",
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def fork_conversation(self, conversation_id: str, *, up_to_message_id: str | None) -> Conversation | None:
        """A new conversation holding copies of the messages BEFORE ``up_to_message_id`` (all of
        them when None). History stays append-only: editing a message means forking here and
        sending the edit in the fork. Copies carry no run ids."""
        src = await self.get_conversation(conversation_id)
        if src is None:
            return None
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY rowid", (conversation_id,)
        )
        if up_to_message_id is not None:
            cut = next((i for i, r in enumerate(rows) if r["id"] == up_to_message_id), None)
            if cut is None:
                raise ValueError("up_to_message_id is not in this conversation")
            rows = rows[:cut]
        fork = await self.create_conversation(kind=ConversationKind.CHAT, title=f"{src.title} (fork)")
        await self.db.execute("UPDATE conversations SET title_auto = 0 WHERE id = ?", (fork.id,))
        last_preview: str | None = None
        for r in rows:
            await self.db.execute(
                "INSERT INTO messages(id, conversation_id, run_id, role, content, reasoning, tool_calls,"
                " tool_call_id, name, partial, created_at) VALUES (?,?,NULL,?,?,?,?,?,?,?,?)",
                (
                    new_id("msg"),
                    fork.id,
                    r["role"],
                    r["content"],
                    r["reasoning"],
                    r["tool_calls"],
                    r["tool_call_id"],
                    r["name"],
                    r["partial"],
                    r["created_at"],
                ),
            )
            if r["role"] in ("user", "assistant") and not r["name"]:
                last_preview = r["content"]
        await self.touch_conversation(fork.id, last_preview)
        return await self.get_conversation(fork.id)

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
        # Named user messages (context, supervisor, summary, transcript) are system notes, not
        # something Arsen typed: they must not become the sidebar preview.
        preview = message.content if message.role in {Role.USER, Role.ASSISTANT} and not message.name else None
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

    # --- provider tool memory -------------------------------------------------------

    async def load_provider_tools(self) -> dict[str, list[ToolSpec]]:
        """What every provider was last seen to offer. Never raises: a stored shape the current
        code cannot read is worth a warning, not a core that will not start."""
        rows = await self.db.fetchall("SELECT provider, specs FROM provider_tools")
        out: dict[str, list[ToolSpec]] = {}
        for row in rows:
            try:
                out[row["provider"]] = [ToolSpec.model_validate(s) for s in json.loads(row["specs"])]
            except Exception as exc:
                log.warning("stored tool list for %s is unreadable (%s); ignoring it", row["provider"], exc)
        return out

    async def save_provider_tools(self, provider: str, specs: list[ToolSpec]) -> None:
        await self.db.execute(
            "INSERT INTO provider_tools(provider, specs, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(provider) DO UPDATE SET specs = excluded.specs, updated_at = excluded.updated_at",
            (provider, json.dumps([s.model_dump(mode="json") for s in specs]), _now()),
        )

    async def forget_provider_tools(self, provider: str) -> None:
        await self.db.execute("DELETE FROM provider_tools WHERE provider = ?", (provider,))

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
