-- Jarvis V2 schema, migration 11: the mail desk's chats get a kind of their own.
--
-- Opening a thread on the mail desk makes (or reopens) a chat beside it. As kind 'chat' those
-- filled Arsen's flat chat list; as 'mail' they sit in a Mail folder like Triage and Scheduled,
-- grouped by mailbox. The key is mail:<account>:<conversation id>; the label is the account.
UPDATE conversations
SET kind = 'mail',
    folder_label = substr(folder_key, 6, instr(substr(folder_key, 6), ':') - 1)
WHERE kind = 'chat' AND folder_key LIKE 'mail:%:%';
