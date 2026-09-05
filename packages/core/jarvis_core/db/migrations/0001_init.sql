-- Jarvis V2 schema, migration 1. Times are ISO-8601 UTC strings; JSON columns hold pydantic dumps.

CREATE TABLE conversations (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    folder_key    TEXT,
    folder_label  TEXT,
    archived      INTEGER NOT NULL DEFAULT 0,
    unread        INTEGER NOT NULL DEFAULT 0,
    preview       TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_conversations_list ON conversations (archived, kind, folder_key, updated_at DESC);

CREATE TABLE messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id          TEXT,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    reasoning       TEXT,
    tool_calls      TEXT,
    tool_call_id    TEXT,
    name            TEXT,
    partial         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_messages_conversation ON messages (conversation_id, created_at, id);
CREATE INDEX idx_messages_run ON messages (run_id);

CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    input_text      TEXT NOT NULL DEFAULT '',
    plan            TEXT,
    budget          TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    steps_used      INTEGER NOT NULL DEFAULT 0,
    usage           TEXT NOT NULL,
    last_seq        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    waiting_reason  TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX idx_runs_status ON runs (status, priority, created_at);
CREATE INDEX idx_runs_conversation ON runs (conversation_id, created_at DESC);

-- Append-only. model.delta events are fan-out only and are NOT stored here.
CREATE TABLE run_events (
    run_id  TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Mutating tool calls carry run_id:step:seq; a replayed key is refused.
CREATE TABLE idempotency (
    key        TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    tool       TEXT NOT NULL,
    result     TEXT,
    created_at TEXT NOT NULL
);
