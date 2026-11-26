-- Jarvis V2 schema, migration 25: projects - several schedules that serve one bigger goal
-- (Arsen, 2026-11-26: "they are separate but part of one bigger task").
--
-- A project is a name, its goal, a brief every one of its schedules is given when it fires, and
-- an optional end after which its schedules pause themselves. A schedule belongs to at most one.
-- Additive only: an older core ignores the table and the column.
CREATE TABLE projects (
    id         TEXT PRIMARY KEY,               -- prj_...
    name       TEXT NOT NULL,
    goal       TEXT NOT NULL DEFAULT '',
    brief      TEXT NOT NULL DEFAULT '',
    ends_at    TEXT,                           -- UTC ISO; NULL = open-ended
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE schedules ADD COLUMN project_id TEXT;
CREATE INDEX schedules_project ON schedules(project_id);
