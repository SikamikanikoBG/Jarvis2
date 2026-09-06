-- Jarvis V2 schema, migration 5: attachments (docs/stories/09_attachments.md).
--
-- One row per uploaded thing, whatever it is: a photo from the phone, a file from the laptop,
-- pasted text, an email thread. `kind` decides how it reaches the model (an image becomes an
-- image part, everything else becomes text), so the chat has ONE attach concept rather than a
-- separate feature per file type.

CREATE TABLE attachments (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      TEXT,          -- set when the message it was sent with is stored
    kind            TEXT NOT NULL, -- image | document | text | email
    name            TEXT NOT NULL,
    mime            TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    path            TEXT,          -- the original on disk, NULL for text-only attachments
    text            TEXT,          -- extracted text (documents, pasted text, email threads)
    meta            TEXT,          -- JSON: width/height, page count, truncation, source
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_attachments_message ON attachments (message_id);
CREATE INDEX idx_attachments_conversation ON attachments (conversation_id, created_at);
