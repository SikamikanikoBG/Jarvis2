-- Jarvis V2 schema, migration 17: the newest decision of an account that has been told it looks
-- stalled (no new mail for triage.stall_alert_hours), so one stall buzzes the phone once.
ALTER TABLE triage_state ADD COLUMN stalled_since TEXT;
