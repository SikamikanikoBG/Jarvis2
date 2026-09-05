"""Agent collaboration: bearer keys, the core as an MCP server, run-and-wait helper."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import Context, FastMCP

from jarvis_core.db import Database
from jarvis_core.engine.bus import Subscriber
from jarvis_proto import CollabKey, ConversationKind, RunKind, RunStatus

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class CollabKeys:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list(self) -> list[CollabKey]:
        rows = await self.db.fetchall("SELECT * FROM collab_keys ORDER BY created_at")
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: Any) -> CollabKey:
        return CollabKey(
            id=r["id"],
            name=r["name"],
            created_at=datetime.fromisoformat(r["created_at"]),
            last_used_at=datetime.fromisoformat(r["last_used_at"]) if r["last_used_at"] else None,
        )

    async def create(self, name: str) -> tuple[str, CollabKey]:
        plain = "jk_" + secrets.token_urlsafe(32)
        kid = "key_" + secrets.token_hex(6)
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            "INSERT INTO collab_keys(id, name, key_hash, created_at, last_used_at) VALUES (?,?,?,?,NULL)",
            (kid, name.strip() or "collaborator", _hash(plain), now),
        )
        return plain, CollabKey(id=kid, name=name.strip() or "collaborator", created_at=datetime.fromisoformat(now))

    async def delete(self, key_id: str) -> None:
        await self.db.execute("DELETE FROM collab_keys WHERE id = ?", (key_id,))

    async def verify(self, plain: str) -> CollabKey | None:
        if not plain:
            return None
        row = await self.db.fetchone("SELECT * FROM collab_keys WHERE key_hash = ?", (_hash(plain),))
        if row is None:
            return None
        await self.db.execute(
            "UPDATE collab_keys SET last_used_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), row["id"])
        )
        return self._row(row)


async def run_and_wait(
    core: Core,
    *,
    text: str,
    key: CollabKey | None,
    conversation_id: str | None = None,
    timeout_s: float | None = None,
) -> tuple[str, str, str, str]:
    """Create a collab run and wait for it. Returns (reply, conversation_id, run_id, status)."""
    folder_key = key.id if key else "owner"
    folder_label = key.name if key else "owner"
    conv = await core.store.get_conversation(conversation_id) if conversation_id else None
    if conv is None:
        title = (text.strip().splitlines()[0] if text.strip() else "collab")[:60]
        conv = await core.store.create_conversation(
            kind=ConversationKind.COLLAB, title=title, folder_key=folder_key, folder_label=folder_label
        )
        from jarvis_proto.events import ConversationUpdated

        core.bus.publish(ConversationUpdated(conversation=conv))
    sub = Subscriber(name=f"collab:{folder_label}")
    sub.conversations.add(conv.id)
    core.bus.attach(sub)
    try:
        run, _ = await core.engine.create_run(text=text, conversation_id=conv.id, kind=RunKind.COLLAB)
        budget = timeout_s or float(run.budget.max_seconds + 30)
        async with asyncio.timeout(budget):
            while True:
                ev = await sub.queue.get()
                if getattr(ev, "run_id", None) != run.id:
                    continue
                if ev.type in {"run.done", "run.failed", "run.cancelled"}:
                    break
    finally:
        core.bus.detach(sub)
    final = await core.store.get_run(run.id)
    status = final.status.value if final else "unknown"
    reply = ""
    if final is not None and final.status is RunStatus.FAILED:
        reply = f"[run failed] {final.error}"
    else:
        msgs = await core.store.list_run_messages(run.id)
        for m in reversed(msgs):
            if m.role.value == "assistant" and m.content.strip():
                reply = m.content
                break
    return reply, conv.id, run.id, status


def build_mcp_server(core: Core) -> FastMCP:
    """The core itself as an MCP server (mounted at /mcp; bearer = collab key or owner token)."""
    mcp = FastMCP("jarvis", stateless_http=True, streamable_http_path="/")

    def _key_from(ctx: Context[Any, Any, Any]) -> CollabKey | None:
        request = getattr(ctx.request_context, "request", None)
        header = request.headers.get("authorization", "") if request is not None else ""
        return getattr(request.state, "collab_key", None) if request is not None and header else None

    @mcp.tool(name="jarvis_chat", description="Send a message to Jarvis and get the full reply (runs tools as needed).")
    async def jarvis_chat(
        message: str, conversation_id: str | None = None, ctx: Context[Any, Any, Any] | None = None
    ) -> dict[str, Any]:
        key = _key_from(ctx) if ctx is not None else None
        reply, conv_id, run_id, status = await run_and_wait(
            core, text=message, key=key, conversation_id=conversation_id
        )
        return {"reply": reply, "conversation_id": conv_id, "run_id": run_id, "status": status}

    @mcp.tool(name="jarvis_status", description="Jarvis health: version, model endpoints, tool providers, queue.")
    async def jarvis_status() -> dict[str, Any]:
        from jarvis_core import __version__

        return {
            "version": __version__,
            "roles": {
                r.value: {"provider": s.provider.value, "model": s.model} for r, s in core.settings.roles.items()
            },
            "tools": core.registry.provider_health(),
            "runs": {"running": len(core.engine.active_run_ids()), "queued": core.engine.queued_count()},
        }

    @mcp.tool(name="jarvis_notes", description="Read Arsen's notes boards (op=boards|list) or pin a note (op=add).")
    async def jarvis_notes(op: str, board: str | None = None, text: str | None = None) -> str:
        if op == "boards":
            boards = await core.boards.list_boards()
            return "\n".join(f"- {b.name} ({b.note_count})" for b in boards) or "No boards."
        if op == "list" and board:
            b = await core.boards.find_board(board)
            if b is None:
                return f"no board {board!r}"
            return "\n".join(f"- {n.text}" for n in await core.boards.list_notes(b.id)) or "empty"
        if op == "add" and board and text:
            b = await core.boards.find_board(board) or await core.boards.create_board(board)
            note = await core.boards.add_note(b.id, text)
            return f"pinned {note.id}"
        return "usage: op=boards | op=list board=<name> | op=add board=<name> text=<text>"

    return mcp


class CollabAuthMiddleware:
    """ASGI wrapper for the mounted MCP app: bearer must be a collab key or the owner token."""

    def __init__(self, app: Any, core: Core) -> None:
        self.app = app
        self.core = core

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        owner = self.core.config.token
        key = None
        if not (owner and token == owner) and not (owner is None and not token):
            key = await self.core.collab_keys.verify(token)
            if key is None:
                body = b'{"error":"unauthorized"}'
                await send(
                    {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]}
                )
                await send({"type": "http.response.body", "body": body})
                return
        scope.setdefault("state", {})["collab_key"] = key
        await self.app(scope, receive, send)
