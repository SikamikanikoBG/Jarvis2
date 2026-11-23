-- Long-term memory, step 1 (memory-track PROPOSAL §6.1): what was said in every conversation, searchable.
-- `said` numbers the messages that are conversation - what Arsen wrote or said, what a meeting's
-- transcript heard, what Jarvis answered - and leaves out tool results, steps that only call a tool,
-- and the core's own notes (context, ledger, plan, supervisor, triage lines). Its own integer key:
-- messages has a TEXT primary key, so its rowid is not stable across a VACUUM.
CREATE TABLE said (
    id         INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE
);

-- Diacritics folded; prefixes indexed, because a Bulgarian word is searched by its stem (Гюзелев, Гюзелева).
CREATE VIRTUAL TABLE said_fts USING fts5(content, tokenize = 'unicode61 remove_diacritics 2', prefix = '3 4');

INSERT INTO said(message_id)
SELECT id FROM messages
WHERE trim(content) <> ''
  AND ((role = 'user' AND (name IS NULL OR name = 'transcript'))
    OR (role = 'assistant' AND (name IS NULL OR name = 'meeting') AND tool_calls IS NULL))
ORDER BY rowid;

INSERT INTO said_fts(rowid, content)
SELECT s.id, m.content FROM said s JOIN messages m ON m.id = s.message_id;

CREATE TRIGGER messages_said AFTER INSERT ON messages
WHEN trim(new.content) <> ''
 AND ((new.role = 'user' AND (new.name IS NULL OR new.name = 'transcript'))
   OR (new.role = 'assistant' AND (new.name IS NULL OR new.name = 'meeting') AND new.tool_calls IS NULL))
BEGIN
    INSERT INTO said(message_id) VALUES (new.id);
    INSERT INTO said_fts(rowid, content) VALUES ((SELECT id FROM said WHERE message_id = new.id), new.content);
END;

-- A deleted conversation takes its messages, and those their place here.
CREATE TRIGGER said_gone AFTER DELETE ON said
BEGIN
    DELETE FROM said_fts WHERE rowid = old.id;
END;
