-- Jarvis V2 schema, migration 9: chats that keep nothing, and chats that clean up after themselves.
--
-- Two flags on the conversation, one derived column for the sweeper.
--
-- incognito: nothing about this chat is remembered anywhere else. The knowledge learner skips
-- it, the titler never reads it (the title stays "Incognito chat"), search never returns it,
-- and the sidebar shows no preview of it. The messages themselves still have to be stored
-- while the chat is alive - the engine resumes runs from the database - which is why an
-- incognito chat always has a ttl as well: it burns on its own.
--
-- ttl_seconds: how long the chat may sit idle before the core deletes it. NULL = kept.
-- expires_at: updated_at + ttl_seconds, maintained on every touch so the sweep is one indexed
-- range read instead of arithmetic over every row. ISO-8601 UTC like the other timestamps.
ALTER TABLE conversations ADD COLUMN incognito INTEGER NOT NULL DEFAULT 0;
ALTER TABLE conversations ADD COLUMN ttl_seconds INTEGER;
ALTER TABLE conversations ADD COLUMN expires_at TEXT;
CREATE INDEX idx_conversations_expires ON conversations (expires_at) WHERE expires_at IS NOT NULL;
