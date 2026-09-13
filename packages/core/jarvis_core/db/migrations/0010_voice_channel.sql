-- Jarvis V2 schema, migration 10: which channel a turn was held on.
--
-- A call with Jarvis is the same conversation as the chat - the same messages, the same memory -
-- so a spoken turn is an ordinary row here with one more column. The transcript draws a headset
-- from it and the export says "(spoken)"; the engine reads nothing from it after the fact.
-- 'text' | 'voice'. Existing rows were all typed.
ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'text';
ALTER TABLE runs ADD COLUMN channel TEXT NOT NULL DEFAULT 'text';
