-- Jarvis V2 schema, migration 8: a chat list Arsen can keep tidy.
--
-- Three things the sidebar could not do: hold folders Arsen names himself, be cleared out in
-- one gesture, or say which chats are still working. Only the first needs storage.

-- Arsen's own filing of his chats. Deliberately separate from conversations.folder_key /
-- folder_label, which the MACHINE fills in (a schedule's name, a triage account, a collab
-- key) and which group the kind-folders. This one is his, and only plain chats use it.
CREATE TABLE conversation_folders (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ON DELETE SET NULL, not CASCADE: deleting a folder must never delete the chats in it. They
-- fall back into the flat list, which is where they were before the folder existed.
ALTER TABLE conversations ADD COLUMN folder_id TEXT REFERENCES conversation_folders(id) ON DELETE SET NULL;
CREATE INDEX idx_conversations_folder ON conversations (folder_id, updated_at DESC);

-- The activity dot reads the live run status of EVERY conversation in one query, on every
-- sidebar list; without this it is a scan of the runs table per row.
CREATE INDEX idx_runs_status_conv ON runs (status, conversation_id);
