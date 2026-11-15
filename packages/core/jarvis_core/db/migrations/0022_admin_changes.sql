-- Jarvis V2 schema, migration 22: the trail of changes to settings, schedules and anything else
-- Jarvis changes through the UI's own routes (docs/stories/14_agent_admin.md, issue #14).
--
-- One row per change, by whoever made it (the UI too, so a revert can tell a later change apart).
-- `fields` is a JSON list of {path, old, new, op, secret}; values are stored in clear because a
-- revert needs them (the settings table holds the same secrets) and are masked whenever a row
-- leaves the core. `request` is what an api change or a proposal will send when applied.
-- Additive only: an older core ignores both tables and the new column.
CREATE TABLE admin_changes (
    id              TEXT PRIMARY KEY,           -- chg_...
    at              TEXT NOT NULL,
    area            TEXT NOT NULL,              -- settings | schedule | api
    target          TEXT NOT NULL,              -- "settings" | schedule id | "PATCH /api/boards/b_1"
    actor           TEXT NOT NULL,              -- arsen | jarvis | system | revert | probe-revert
    actor_label     TEXT NOT NULL DEFAULT '',   -- snapshot: survives the schedule or chat being deleted
    run_id          TEXT,
    run_kind        TEXT,
    conversation_id TEXT,
    call_id         TEXT,
    fields          TEXT NOT NULL DEFAULT '[]',
    risk            TEXT NOT NULL DEFAULT '[]',
    reason          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,              -- applied | probing | proposed | auto_reverted | reverted | discarded | expired
    rev             INTEGER,
    reverts         TEXT,
    reverted_by     TEXT,
    note            TEXT NOT NULL DEFAULT '',
    request         TEXT,                       -- JSON {method, path, body} or {set, add, remove}
    probe_deadline  TEXT
);
CREATE INDEX admin_changes_at ON admin_changes(at);
CREATE INDEX admin_changes_conv ON admin_changes(conversation_id, at);

-- The settings revision: one row, bumped by every saved change. Its own table, not a settings
-- key, so a core rolled back to before this migration never tries to validate it.
CREATE TABLE settings_meta (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    rev INTEGER NOT NULL
);
INSERT INTO settings_meta(id, rev) VALUES (1, 0);

-- Who last wrote a schedule's prompt: NULL = Arsen; a run id = a run nobody was asking from.
ALTER TABLE schedules ADD COLUMN prompt_by TEXT;
