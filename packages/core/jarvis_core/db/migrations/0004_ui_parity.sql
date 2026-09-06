-- Jarvis V2 schema, migration 4: chat-surface parity (docs/WAVE2.md, slice 0).

-- Pinned conversations sort first in the sidebar.
ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
-- 1 while the title is machine-made (first line of the first message, then the classifier's
-- 3-6 words after the first reply). A rename by Arsen sets it to 0 and nothing touches it again.
ALTER TABLE conversations ADD COLUMN title_auto INTEGER NOT NULL DEFAULT 1;
