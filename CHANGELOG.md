# Changelog

Notable changes to Jarvis2. Older history lives in the commit log, `docs/journal.md` and `docs/WAVE2.md`.

## 2026-10-30 — core 2.0.0a106, proto 2.0.0a37, web 2.0.0-alpha.86

### Added
- **Delegated tasks** ([#10](https://github.com/SikamikanikoBG/Jarvis2/issues/10), story
  `docs/stories/13_delegated_tasks.md`), behind `settings.tasks.enabled` (Settings → Tasks, off by default):
  - Jarvis hands longer work to a background task and answers in one line within seconds; the chat stays free.
  - A task card in the chat that asked (state, Definition of Done, assumptions, brief, cancel, steer,
    approve/deny) and a `Tasks · N` chip; the task's own work conversation opens from the card.
  - The report lands in the asking chat with the result itself (long results attached) and a quiet push.
  - "Done" only with evidence the code checked in the task's own log; otherwise "claimed, not verified".
  - In a task every non-read-only step waits for Arsen's approval, whatever `confirmations.mode` says;
    shell and file writes are not offered to tasks; approvals expire; a denied step is not asked again.
  - Honest take / ask / decline from real health (what is down or not configured).
  - On a call: asks that take work are noted (live list on the call screen), read back when Arsen wraps
    up, started exactly once after hang-up or a dropped call, and reported after the call.
  - One task runs at a time; none starts during a call and a running one pauses between steps.

### Changed
- The stored call style lost its "never after the call" sentence (Arsen's other edits kept).
- On a call, answers with no tool result behind them are no longer learned into the knowledge graph.
- Status shows web search / mail as not ok when nothing is configured.
- The desktop top bar never outgrows the window.

### Fixed
- One-off reminders fired two hours late (a `+02:00` time compared as text against UTC).
- After a restart, an approved but interrupted outward call was re-run without asking again.
- A shutdown in the middle of a model step was recorded as a cancel instead of an interruption.
- A partial nested settings PATCH reset sibling fields.
- `RunKind.MAIL` had no priority, lane or budget.

### Known issues
- A correct task can still end "partly done" when a DoD item describes the output itself (N-8).
- A table sent before the task's last step can reach the report only as prose.
- The task status count can over-report running tasks while one waits for approval.
