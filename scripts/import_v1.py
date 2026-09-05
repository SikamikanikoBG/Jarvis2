"""Import V1 data into a V2 database (idempotent via the import_map table).

    uv run python scripts/import_v1.py --db R:\\Projects\\Jarvis\\jarvis.db --skills R:\\Projects\\Jarvis\\skills \
        --home ./data [--dry-run] [--no-conversations]

Imports: notes boards + sticky notes, knowledge graph (entities, aliases, edges, mentions),
skills (markdown files, frontmatter added when missing), scheduler jobs → schedules (best
effort; unmapped jobs are listed), conversations + messages as an "Archive (V1)" folder.
Nothing is deleted or modified in V1. Run it against a COPY of the ardi memory-service DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "proto"))

from jarvis_core.db import Database
from jarvis_core.features.boards import BoardStore
from jarvis_core.features.knowledge import KnowledgeStore
from jarvis_core.features.schedules import ScheduleStore
from jarvis_core.features.skills import SkillStore
from jarvis_proto import ConversationKind, Message, Role, new_id

_V1_TYPE = {"person": "person", "project": "project", "org": "org", "topic": "topic", "place": "place"}
_COLORS = {"yellow", "blue", "green", "pink", "grey"}


class Importer:
    def __init__(self, v1: sqlite3.Connection, db: Database, *, dry_run: bool) -> None:
        self.v1 = v1
        self.db = db
        self.dry = dry_run
        self.counts: dict[str, int] = {}

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    async def mapped(self, source: str, v1_id: str) -> str | None:
        row = await self.db.fetchone("SELECT v2_id FROM import_map WHERE source = ? AND v1_id = ?", (source, v1_id))
        return row["v2_id"] if row else None

    async def remember(self, source: str, v1_id: str, v2_id: str) -> None:
        if not self.dry:
            await self.db.execute(
                "INSERT OR REPLACE INTO import_map(source, v1_id, v2_id) VALUES (?,?,?)", (source, v1_id, v2_id)
            )

    def has(self, table: str) -> bool:
        return (
            self.v1.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            is not None
        )

    # --- boards --------------------------------------------------------------------------

    async def boards(self) -> None:
        if not self.has("note_boards"):
            return
        store = BoardStore(self.db)
        for b in self.v1.execute("SELECT * FROM note_boards ORDER BY position, created_at"):
            if await self.mapped("board", b["id"]):
                continue
            self.bump("boards")
            if self.dry:
                continue
            board = await store.create_board(b["name"])
            await self.remember("board", b["id"], board.id)
        if not self.has("sticky_notes"):
            return
        for n in self.v1.execute("SELECT * FROM sticky_notes ORDER BY board_id, position"):
            if await self.mapped("note", n["id"]):
                continue
            board_id = await self.mapped("board", n["board_id"])
            if board_id is None:
                continue
            self.bump("notes")
            if self.dry:
                continue
            color = n["color"] if n["color"] in _COLORS else "yellow"
            note = await store.add_note(board_id, n["content"], color=color, from_message_id=n["source_msg_id"] or None)  # type: ignore[arg-type]
            await self.remember("note", n["id"], note.id)

    # --- knowledge -----------------------------------------------------------------------

    async def knowledge(self) -> None:
        if not self.has("entities"):
            return
        store = KnowledgeStore(self.db)
        for e in self.v1.execute("SELECT * FROM entities"):
            key = str(e["id"])
            if await self.mapped("entity", key):
                continue
            aliases = (
                [
                    a["alias"]
                    for a in self.v1.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?", (e["id"],))
                ]
                if self.has("entity_aliases")
                else []
            )
            self.bump("entities")
            if self.dry:
                continue
            ent = await store.upsert(e["canonical_name"], type=_V1_TYPE.get(e["type"], "thing"), aliases=aliases[:12])
            await self.db.execute(
                "UPDATE kg_entities SET mention_count = mention_count + ? WHERE id = ?",
                (int(e["mention_count"] or 0), ent.id),
            )
            await self.remember("entity", key, ent.id)
        if self.has("edges"):
            for r in self.v1.execute("SELECT * FROM edges WHERE valid_to IS NULL"):
                src = await self.mapped("entity", str(r["src_entity_id"]))
                dst = await self.mapped("entity", str(r["dst_entity_id"]))
                if not src or not dst:
                    continue
                self.bump("edges")
                if not self.dry:
                    await store.add_edge(
                        src, dst, r["rel_type"], weight=float(r["confidence"] or 1.0), evidence=r["source_id"] or None
                    )
        if self.has("mentions"):
            for m in self.v1.execute("SELECT * FROM mentions ORDER BY ts DESC LIMIT 5000"):
                ent = await self.mapped("entity", str(m["entity_id"]))
                if not ent:
                    continue
                self.bump("mentions")
                if not self.dry:
                    await self.db.execute(
                        "INSERT INTO kg_mentions(entity_id, conversation_id, message_id, snippet, at) VALUES (?,?,?,?,?)",
                        (ent, None, None, (m["snippet"] or "")[:200] or None, m["ts"] or datetime.now(UTC).isoformat()),
                    )

    # --- skills --------------------------------------------------------------------------

    async def skills(self, source_dir: Path | None, home: Path) -> None:
        if source_dir is None or not source_dir.exists():
            return
        store = SkillStore(home / "skills", self.db)
        for path in sorted(source_dir.glob("*.md")):
            name = re.sub(r"[^a-z0-9_-]", "_", path.stem.lower())[:64].strip("_") or "skill"
            content = path.read_text(encoding="utf-8", errors="replace")
            meta, _ = SkillStore.parse(content) if content.startswith("---") else ({}, content)
            if "description" not in meta:
                first = next((ln.strip("# ").strip() for ln in content.splitlines() if ln.strip()), name)
                content = f"---\ndescription: {json.dumps(first[:140])}\n---\n{content}"
            self.bump("skills")
            if not self.dry:
                await store.put(name, content)

    # --- schedules -----------------------------------------------------------------------

    async def schedules(self, tz: str, *, enable: bool = False) -> tuple[list[str], list[str]]:
        """Returns (unmapped, was_enabled_in_v1). Schedules are imported DISABLED unless
        ``enable`` — importing 14 live automations that start firing at Arsen unattended is
        not something a data migration should decide."""
        unmapped: list[str] = []
        live: list[str] = []
        if not self.has("scheduler_jobs"):
            return unmapped, live
        store = ScheduleStore(self.db)
        for j in self.v1.execute("SELECT * FROM scheduler_jobs"):
            if await self.mapped("schedule", j["id"]):
                continue
            data: dict[str, Any] = json.loads(j["data"] or "{}")
            name = str(data.get("name") or j["id"]).strip()
            prompt = str(data.get("message") or data.get("prompt") or data.get("text") or "").strip()
            cron, at = _when(data)
            if not prompt or not (cron or at):
                unmapped.append(f"{name}: {json.dumps(data, ensure_ascii=False)[:150]}")
                continue
            was_enabled = bool(j["enabled"]) and str(data.get("enabled", "True")).lower() != "false"
            if was_enabled:
                live.append(f"{name} — {cron or ('once ' + at.isoformat() if at else '?')}")
            self.bump("schedules")
            if self.dry:
                continue
            s = await store.create(
                name=name[:80], prompt=prompt, cron=cron, at=at, tz=tz, enabled=bool(enable and was_enabled)
            )
            await self.remember("schedule", j["id"], s.id)
        return unmapped, live

    # --- conversations -------------------------------------------------------------------

    async def conversations(self) -> None:
        if not self.has("conversations") or not self.has("conversation_messages"):
            return
        for c in self.v1.execute("SELECT * FROM conversations ORDER BY created_at"):
            if await self.mapped("conversation", c["id"]):
                continue
            msgs = self.v1.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY timestamp, id", (c["id"],)
            ).fetchall()
            if not msgs:
                continue
            self.bump("conversations")
            self.bump("messages", len(msgs))
            if self.dry:
                continue
            conv_id = new_id("conv")
            await self.db.execute(
                "INSERT INTO conversations(id, kind, title, folder_key, folder_label, archived, unread, preview, message_count,"
                " created_at, updated_at) VALUES (?,?,?,?,?,0,0,?,?,?,?)",
                (
                    conv_id,
                    ConversationKind.ARCHIVE.value,
                    (c["name"] or "V1 conversation")[:80],
                    "v1",
                    "Archive (V1)",
                    (msgs[-1]["content"] or "")[:160],
                    len(msgs),
                    c["created_at"],
                    c["last_activity_at"] or c["created_at"],
                ),
            )
            for m in msgs:
                role = {"user": Role.USER, "assistant": Role.ASSISTANT, "system": Role.SYSTEM, "tool": Role.TOOL}.get(
                    m["role"], Role.USER
                )
                msg = Message(role=role, content=m["content"] or "", name=m["tool_name"], conversation_id=conv_id)
                msg.created_at = datetime.fromisoformat(m["timestamp"]) if m["timestamp"] else datetime.now(UTC)
                msg.id = new_id("msg")
                await self.db.execute(
                    "INSERT INTO messages(id, conversation_id, run_id, role, content, reasoning, tool_calls, tool_call_id, name,"
                    " partial, created_at) VALUES (?,?,NULL,?,?,NULL,NULL,NULL,?,0,?)",
                    (msg.id, conv_id, role.value, msg.content, msg.name, msg.created_at.isoformat()),
                )
            await self.remember("conversation", c["id"], conv_id)


def _when(data: dict[str, Any]) -> tuple[str | None, datetime | None]:
    """V1 job → (cron, at). V1 shape: type=daily|interval|once, daily_time 'HH:MM',
    days_of_week as Python weekdays (Mon=0), interval_minutes, once → schedule=ISO."""
    if data.get("cron"):
        return str(data["cron"]), None
    kind = str(data.get("type") or "").strip().lower()
    if kind == "once" or (not kind and str(data.get("schedule", "")).count("-") >= 2):
        raw = str(data.get("schedule") or data.get("at") or "").strip()
        try:
            return None, datetime.fromisoformat(raw)
        except ValueError:
            return None, None
    if kind == "interval" or data.get("interval_minutes"):
        try:
            minutes = int(data.get("interval_minutes") or data.get("schedule") or 0)
        except (TypeError, ValueError):
            return None, None
        if minutes <= 0:
            return None, None
        if minutes % 60 == 0 and minutes < 24 * 60:
            return f"0 */{minutes // 60} * * *", None
        return f"*/{max(1, min(minutes, 59))} * * * *", None
    raw_time = str(data.get("daily_time") or data.get("schedule") or data.get("time") or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw_time)
    if not m:
        return None, None
    hh, mm = int(m.group(1)), int(m.group(2))
    days = data.get("days_of_week")
    if isinstance(days, list) and days:
        # Python weekday (Mon=0 … Sun=6) → cron day-of-week (Sun=0 … Sat=6).
        cron_days = sorted({(int(d) + 1) % 7 for d in days if isinstance(d, int | str) and str(d).isdigit()})
        if cron_days and len(cron_days) < 7:
            return f"{mm} {hh} * * {','.join(str(d) for d in cron_days)}", None
    return f"{mm} {hh} * * *", None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="V1 jarvis.db (a copy)")
    ap.add_argument("--skills", default=None, help="V1 skills directory")
    ap.add_argument("--home", default="./data", help="V2 JARVIS_HOME")
    ap.add_argument("--tz", default="Europe/Sofia")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-conversations", action="store_true")
    ap.add_argument(
        "--enable-schedules",
        action="store_true",
        help="import schedules already enabled (default: import them disabled so nothing fires unattended)",
    )
    args = ap.parse_args()

    v1 = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
    v1.row_factory = sqlite3.Row
    home = Path(args.home).resolve()
    db = Database(home / "jarvis2.db")
    await db.open()
    imp = Importer(v1, db, dry_run=args.dry_run)
    try:
        await imp.boards()
        await imp.knowledge()
        await imp.skills(Path(args.skills) if args.skills else None, home)
        unmapped, live = await imp.schedules(args.tz, enable=args.enable_schedules)
        if not args.no_conversations:
            await imp.conversations()
    finally:
        await db.close()
        v1.close()
    mode = "DRY RUN — nothing written" if args.dry_run else "imported"
    print(f"{mode}: " + (", ".join(f"{k}={v}" for k, v in sorted(imp.counts.items())) or "nothing to import"))
    if live:
        state = "ENABLED" if args.enable_schedules else "imported DISABLED (pass --enable-schedules to keep them on)"
        print(f"\n{len(live)} schedule(s) were live in V1 — {state}:")
        for line in live:
            print("  -", line)
    if unmapped:
        print(f"\n{len(unmapped)} scheduler job(s) could not be mapped — recreate them by hand if you still want them:")
        for line in unmapped:
            print("  -", line)


if __name__ == "__main__":
    asyncio.run(main())
