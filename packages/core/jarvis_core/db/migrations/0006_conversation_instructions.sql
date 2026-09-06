-- Per-conversation instructions: a persona or a standing rule that applies to THIS chat only.
--
-- It lives in the system message, appended after the parts every conversation shares (rules,
-- personality, boards) and before the date line, so a chat that has none is byte-identical to
-- today and a chat that has some still reuses the shared cached prefix up to that point.
ALTER TABLE conversations ADD COLUMN instructions TEXT NOT NULL DEFAULT '';
