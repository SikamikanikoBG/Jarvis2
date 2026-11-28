-- Jarvis V2 schema, migration 29: the guidance in progress (features/navigation.py, 2026-11-28).
-- One row: the active route and where he is on it, so a core restart mid-walk picks it up again.
CREATE TABLE nav_session (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
