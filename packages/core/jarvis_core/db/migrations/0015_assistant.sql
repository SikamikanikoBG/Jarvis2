-- Jarvis V2 schema, migration 15 (docs/stories/12_assistant.md).

-- The day journal: where each conversation stands today, rewritten in the background when one of
-- its runs ends. One row per conversation per (local) day; `waiting` is what it needs from
-- Arsen ('' = nothing); `quiet` marks a routine run that found nothing worth telling.
CREATE TABLE conversation_digests (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,
    summary         TEXT NOT NULL,
    waiting         TEXT NOT NULL DEFAULT '',
    quiet           INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, day)
);
CREATE INDEX conversation_digests_day ON conversation_digests(day, updated_at);

-- The blocks a turn's context message was made of. A block identical to one already given in the
-- conversation is written as a reference (`carried`), and the earlier copy stays whole while a
-- later turn refers to it; `body` is always the full text, so a reference whose copy has been
-- trimmed away can be written out again.
CREATE TABLE context_blocks (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    key        TEXT NOT NULL,
    digest     TEXT NOT NULL,
    body       TEXT NOT NULL,
    carried    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, ord)
);
CREATE INDEX context_blocks_digest ON context_blocks(digest);
