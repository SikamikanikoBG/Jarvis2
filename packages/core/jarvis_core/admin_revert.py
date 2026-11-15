"""Break-glass: undo a settings change straight in the database, with the core down or unreachable.

    python -m jarvis_core.admin_revert --list
    python -m jarvis_core.admin_revert --last          # the newest settings change in force
    python -m jarvis_core.admin_revert chg_1a2b...      # that one

The normal ways back need no model either - the change card's Revert and Settings -> Changes ->
Undo call ``POST /api/settings/changes/{id}/revert`` - but they need a core that answers. This is
for the night it does not (a bad MCP spec that hangs the start, docs/stories/14_agent_admin.md).
It writes the old values of the change's fields back into the settings rows, marks the change
reverted and bumps the revision; restart the core afterwards. On ardi: scripts/admin_revert.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from jarvis_core.config import CoreConfig
from jarvis_core.features.admin import _undo  # pyright: ignore[reportPrivateUsage] - the same undo as the core's
from jarvis_proto import ChangeField, Settings
from jarvis_proto import settings_paths as sp


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _settings(conn: sqlite3.Connection) -> dict[str, object]:
    data = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")}
    return Settings.model_validate(data).model_dump(mode="json")


def revert(conn: sqlite3.Connection, change_id: str) -> list[str]:
    row = conn.execute("SELECT * FROM admin_changes WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no change {change_id}")
    if row["area"] != "settings":
        raise SystemExit(f"{change_id} is a {row['area']} change; only settings are undone here")
    if row["status"] not in ("applied", "probing"):
        raise SystemExit(f"{change_id} is {row['status']}; nothing to undo")
    fields = [ChangeField.model_validate(f) for f in json.loads(row["fields"])]
    draft = _settings(conn)
    for f in reversed(fields):
        _undo(draft, f)  # type: ignore[arg-type]
    new = Settings.model_validate(draft).model_dump(mode="json")
    keys = sorted({sp.parse(f.path)[0].name or "" for f in fields})
    now = datetime.now(UTC).isoformat()
    with conn:
        for key in keys:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(new[key]), now),
            )
        conn.execute("UPDATE settings_meta SET rev = rev + 1 WHERE id = 1")
        conn.execute(
            "UPDATE admin_changes SET status = 'reverted', note = ? WHERE id = ?",
            ((row["note"] + " " if row["note"] else "") + "undone by admin_revert (break-glass)", change_id),
        )
    return [f.path for f in fields]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("change_id", nargs="?")
    ap.add_argument("--last", action="store_true", help="the newest settings change still in force")
    ap.add_argument("--list", action="store_true", help="the last 20 changes")
    ap.add_argument("--db", type=Path, default=None, help="default: $JARVIS_HOME/jarvis2.db")
    args = ap.parse_args(argv)
    conn = _connect(args.db or CoreConfig().db_path)
    if args.list:
        for r in conn.execute("SELECT * FROM admin_changes ORDER BY at DESC LIMIT 20"):
            paths = ", ".join(f["path"] for f in json.loads(r["fields"]))
            print(f"{r['id']}  {r['at'][:19]}  {r['area']:<8} {r['status']:<13} {r['actor']:<8} {paths[:80]}")
        return 0
    change_id = args.change_id
    if args.last:
        row = conn.execute(
            "SELECT id FROM admin_changes WHERE area = 'settings' AND status IN ('applied', 'probing')"
            " ORDER BY at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("no settings change in force", file=sys.stderr)
            return 1
        change_id = row["id"]
    if not change_id:
        ap.print_help()
        return 2
    paths = revert(conn, change_id)
    print(f"reverted {change_id}: {', '.join(paths)} - restart the core to load it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
