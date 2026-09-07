-- What each tool provider was last known to offer, so a core restart does not erase a
-- machine's capabilities while that machine happens to be asleep.
--
-- 2026-09-07: jarvis-host died at 03:28 and the core restarted at 07:33 with the laptop
-- unreachable. The in-memory tool memory started empty, so all 34 workocholic tools were
-- absent from the prompt and `workocholic.outlook_send` came back as "unknown tool" — the run
-- told Arsen it could not send his digest and then guessed two tool names that never existed.
-- The tool list is also part of the prompt prefix, so remembering it keeps every cached
-- conversation cached across a restart.
CREATE TABLE IF NOT EXISTS provider_tools(
    provider   TEXT PRIMARY KEY,
    specs      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
