-- Jarvis V2 schema, migration 2: boards, knowledge graph, summaries, skills state,
-- schedules, triage, collab keys, meetings, V1 import map.

CREATE TABLE boards (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE notes (
    id              TEXT PRIMARY KEY,
    board_id        TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT 'yellow',
    from_message_id TEXT,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_notes_board ON notes (board_id, position);

CREATE TABLE kg_entities (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'thing',
    summary       TEXT NOT NULL DEFAULT '',
    mention_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_kg_entities_name ON kg_entities (name COLLATE NOCASE);

CREATE TABLE kg_aliases (
    alias     TEXT NOT NULL COLLATE NOCASE,
    entity_id TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    PRIMARY KEY (alias)
);

CREATE TABLE kg_edges (
    src        TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    dst        TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation   TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    evidence   TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (src, dst, relation)
);
CREATE INDEX idx_kg_edges_dst ON kg_edges (dst);

CREATE TABLE kg_mentions (
    entity_id       TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    conversation_id TEXT,
    message_id      TEXT,
    snippet         TEXT,
    at              TEXT NOT NULL
);
CREATE INDEX idx_kg_mentions_entity ON kg_mentions (entity_id, at DESC);

CREATE TABLE conversation_summaries (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    up_to_message_id TEXT NOT NULL,
    text             TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_summaries_conversation ON conversation_summaries (conversation_id, created_at DESC);

CREATE TABLE skills_state (
    name    TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE schedules (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    cron           TEXT,
    at             TEXT,
    tz             TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    catch_up       TEXT NOT NULL DEFAULT 'skip',
    think          INTEGER,
    think_level    TEXT,
    next_fire      TEXT,
    last_fired_for TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- One row per (schedule, slot): the UNIQUE key is what makes a double fire impossible.
CREATE TABLE schedule_fires (
    schedule_id     TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    scheduled_for   TEXT NOT NULL,
    run_id          TEXT,
    conversation_id TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (schedule_id, scheduled_for)
);

CREATE TABLE triage_state (
    account         TEXT PRIMARY KEY,
    cursor          TEXT,
    day             TEXT,
    processed_today INTEGER NOT NULL DEFAULT 0,
    routed_today    INTEGER NOT NULL DEFAULT 0,
    last_run_at     TEXT,
    last_error      TEXT
);

CREATE TABLE triage_decisions (
    entry_id  TEXT NOT NULL,
    account   TEXT NOT NULL,
    category  TEXT,
    action    TEXT,
    run_id    TEXT,
    at        TEXT NOT NULL,
    PRIMARY KEY (entry_id, account)
);

CREATE TABLE collab_keys (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE meetings (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    host            TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    summary_run_id  TEXT
);

CREATE TABLE meeting_segments (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    t0         REAL NOT NULL,
    t1         REAL NOT NULL,
    text       TEXT NOT NULL,
    PRIMARY KEY (meeting_id, seq)
);

CREATE TABLE meeting_frames (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    at         REAL NOT NULL,
    path       TEXT NOT NULL,
    ocr        TEXT,
    PRIMARY KEY (meeting_id, seq)
);

CREATE TABLE import_map (
    source TEXT NOT NULL,
    v1_id  TEXT NOT NULL,
    v2_id  TEXT NOT NULL,
    PRIMARY KEY (source, v1_id)
);
