-- Jarvis V2 schema, migration 13: a ring by default (settings.call_me_default) is told apart from
-- one he asked for: the default one stays quiet while he is watching the chat.
ALTER TABLE runs ADD COLUMN call_me_default INTEGER NOT NULL DEFAULT 0;
