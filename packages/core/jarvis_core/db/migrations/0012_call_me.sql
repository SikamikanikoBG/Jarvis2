-- Jarvis V2 schema, migration 12: Jarvis calls back (docs/stories/11_call_me.md).
--
-- call_me is a property of the run, not of the chat: "ring me when THIS is done". A resumed run
-- keeps it, so a restart does not swallow the ring.
ALTER TABLE runs ADD COLUMN call_me INTEGER NOT NULL DEFAULT 0;

-- The devices that ring: one row per browser that turned web push on (features/webpush.py).
-- endpoint is the push service's URL for that browser; p256dh and auth are its RFC 8291 keys.
CREATE TABLE push_devices (
    endpoint     TEXT PRIMARY KEY,
    p256dh       TEXT NOT NULL,
    auth         TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    last_ok_at   TEXT
);

-- The core's own VAPID identity: one P-256 key, made on first use. It lives here and not in the
-- settings so it never travels to the UI, and so a settings reset cannot silently orphan every
-- subscribed phone (a browser's subscription is bound to the public half).
CREATE TABLE push_identity (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    private_pem TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
