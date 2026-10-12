-- Jarvis V2 schema, migration 14: whether a scheduled task rings Arsen when it ends -
-- 'default' (settings.call_me_default decides), 'always' or 'never'.
ALTER TABLE schedules ADD COLUMN call_me TEXT NOT NULL DEFAULT 'default';
