"""Long-term memory: recall of what was said, over every conversation (memory-track PROPOSAL §6).

Step 1: the words themselves. ``said`` (migration 0023) indexes what Arsen and Jarvis said to each
other, and what meetings heard, in full text; ``memory.recall`` searches it by words and by time.
Before it, a chat could only reach what still stood in its window: asked on 17 Nov what he and
Gyuzelev had said the Friday before, Jarvis searched the graph, the notes, OneNote and the mail and
answered that it had no record - the talk was in the same conversation, 3,000 messages up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.current import current_run_id
from jarvis_core.features.sessions import handle_of
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import ConversationKind, ToolResult

if TYPE_CHECKING:
    import aiosqlite

    from jarvis_core.app import Core

#: A match is read with what was said around it: this many messages either side.
AROUND = 2
#: One message as recall shows it: a match nearly whole (he tells a meeting in one long breath),
#: what stands around it cut short. The rest is a sessions.read away.
MATCH_CHARS = 2_400
LINE_CHARS = 500
#: The whole result: a few exchanges, not a transcript.
MAX_CHARS = 9_000


#: Conversations the core runs by itself: found by their words, never what "that morning" means.
_MACHINE_KINDS = (ConversationKind.SCHEDULED, ConversationKind.TRIAGE, ConversationKind.TASK)


class _RecallArgs(BaseModel):
    words: list[str] = Field(
        default_factory=list,
        description=(
            "What to look for: names and key words, each matched as the beginning of a word, any of them. They "
            "match only as they were said, so give each in every language he speaks (a question asked in English "
            "is often about a talk in Bulgarian, and the other way round) and every spelling: Cyrillic and Latin, "
            "short name and family name, one word or two."
        ),
    )
    since: str = Field(default="", description="First day, YYYY-MM-DD (his time zone). Empty = from the beginning.")
    until: str = Field(default="", description="Last day, YYYY-MM-DD, inclusive. Empty = until now.")
    limit: int = Field(default=6, ge=1, le=20, description="How many exchanges")


def match_query(words: list[str]) -> str:
    """FTS5 syntax for "any of these, as prefixes"; a phrase stays together. Quoted, so nothing the
    model writes is read as an operator."""
    terms: list[str] = []
    for phrase in words:
        # Split as the tokenizer does: on anything that is neither a letter nor a digit.
        if tokens := "".join(ch if ch.isalnum() else " " for ch in phrase).split():
            terms.append(" + ".join(f'"{t}"' for t in tokens) + "*")
    return " OR ".join(dict.fromkeys(terms))


def day_bounds(since: str, until: str, tz: ZoneInfo) -> tuple[str | None, str | None]:
    """His days as UTC instants in the form messages carry. Raises ValueError on a bad date."""

    def at(day: date) -> str:
        return datetime.combine(day, time.min, tz).astimezone(UTC).isoformat()

    lo = at(date.fromisoformat(since)) if since.strip() else None
    hi = at(date.fromisoformat(until) + timedelta(days=1)) if until.strip() else None
    return lo, hi


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def search(
        self, words: list[str], *, lo: str | None, hi: str | None, limit: int, exclude_run: str | None = None
    ) -> list[aiosqlite.Row]:
        """The messages that match best, with their conversation. A private chat never appears, and
        neither does the run that is asking. Without words: the window's messages, oldest first, in
        the conversations he takes part in."""
        where = ["c.incognito = 0"]
        params: list[object] = []
        query = match_query(words)
        if query:
            where.append("said_fts MATCH ?")
            params.append(query)
        else:
            where.append("c.kind NOT IN (?, ?, ?)")
            params.extend(k.value for k in _MACHINE_KINDS)
        if lo:
            where.append("m.created_at >= ?")
            params.append(lo)
        if hi:
            where.append("m.created_at < ?")
            params.append(hi)
        if exclude_run:
            where.append("(m.run_id IS NULL OR m.run_id <> ?)")
            params.append(exclude_run)
        source = "said_fts JOIN said s ON s.id = said_fts.rowid" if query else "said s"
        order = "bm25(said_fts), s.id DESC" if query else "m.created_at, s.id"
        return await self.db.fetchall(
            "SELECT s.id, m.conversation_id, m.created_at, m.role, m.name, m.channel, m.content, c.title, c.kind"
            f" FROM {source} JOIN messages m ON m.id = s.message_id"
            " JOIN conversations c ON c.id = m.conversation_id"
            f" WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
            (*params, limit),
        )

    async def around(
        self, said_id: int, conversation_id: str, *, n: int = AROUND, exclude_run: str | None = None
    ) -> list[aiosqlite.Row]:
        """The message and ``n`` said before and after it in its conversation, in order."""
        sql = (
            "SELECT s.id, m.conversation_id, m.created_at, m.role, m.name, m.channel, m.content"
            " FROM said s JOIN messages m ON m.id = s.message_id"
            " WHERE m.conversation_id = ? AND (m.run_id IS NULL OR m.run_id IS NOT ?)"
        )
        where = (conversation_id, exclude_run)
        before = await self.db.fetchall(f"{sql} AND s.id <= ? ORDER BY s.id DESC LIMIT ?", (*where, said_id, n + 1))
        after = await self.db.fetchall(f"{sql} AND s.id > ? ORDER BY s.id LIMIT ?", (*where, said_id, n))
        return [*reversed(before), *after]


class MemoryTools(BuiltinProvider):
    name = "memory"

    def __init__(self, core: Core) -> None:
        self._core = core
        self.store = MemoryStore(core.db)
        super().__init__()

    def _who(self, row: aiosqlite.Row, kind: str) -> str:
        if row["role"] == "assistant":
            return "Jarvis"
        if row["name"] == "transcript":
            return "heard in a meeting"
        # In a conversation the core runs by itself, the user message is the job it was given.
        return "the job" if kind in _MACHINE_KINDS else self._core.settings.user_name

    @tool(
        "memory.recall",
        description=(
            "Search what was said in every past conversation, this one's older messages included - chats, calls, "
            "meetings, scheduled runs - by words and by day. Use it whenever he refers to something from before "
            "that is not in front of you: a talk, a person, a promise, 'as I told you', 'last Friday'. Search it "
            "before you say you have no record; nothing found with one spelling is not the same as nothing said."
        ),
        args=_RecallArgs,
        read_only=True,
        idempotent=True,
        owner_only=True,
    )
    async def _recall(
        self, words: list[str] | None = None, since: str = "", until: str = "", limit: int = 6
    ) -> ToolResult:
        words = words or []
        tz = ZoneInfo(self._core.settings.timezone)
        try:
            lo, hi = day_bounds(since, until, tz)
        except ValueError:
            return ToolResult.failure("since and until are days: YYYY-MM-DD")
        if not match_query(words) and not (lo or hi):
            return ToolResult.failure("say what to look for (words), when (since/until), or both")
        here = current_run_id.get()
        hits = await self.store.search(words, lo=lo, hi=hi, limit=limit * 3, exclude_run=here)
        if not hits:
            return ToolResult.empty(
                f"Nothing said matches {', '.join(words) or 'that'}"
                + (f" between {since or 'the beginning'} and {until or 'now'}" if lo or hi else "")
                + ". Try other spellings or a wider time."
            )
        matched = {h["id"] for h in hits}
        blocks: list[str] = []
        shown: set[int] = set()
        used = 0
        for hit in hits:
            if hit["id"] in shown or len(blocks) >= limit:
                continue
            near = await self.store.around(hit["id"], hit["conversation_id"], exclude_run=here)
            rows = [
                r
                for r in near
                if r["id"] not in shown and (not lo or r["created_at"] >= lo) and (not hi or r["created_at"] < hi)
            ]
            title = " ".join(str(hit["title"]).split())
            lines = [f"## @{handle_of(title)} - {title}"]
            for r in rows:
                when = datetime.fromisoformat(r["created_at"])
                when = (when.replace(tzinfo=UTC) if when.tzinfo is None else when).astimezone(tz)
                spoken = ", on a call" if r["channel"] == "voice" else ""
                text = " ".join(str(r["content"]).split())
                cap = MATCH_CHARS if r["id"] in matched else LINE_CHARS
                if len(text) > cap:
                    text = text[:cap] + "…"
                lines.append(f"[{when:%a %Y-%m-%d %H:%M}{spoken}] {self._who(r, hit['kind'])}: {text}")
            block = "\n".join(lines)
            if used + len(block) > MAX_CHARS and blocks:
                break
            shown.update(r["id"] for r in rows)
            used += len(block)
            blocks.append(block)
        return ToolResult.data("\n\n".join(blocks), count=len(blocks))
