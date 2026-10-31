# Changelog

Notable changes to Jarvis2. Older history lives in the commit log, `docs/journal.md` and `docs/WAVE2.md`.

## 2026-10-31 — core 2.0.0a108, proto 2.0.0a38, web 2.0.0-alpha.87

### Changed
- **Task approvals follow your confirmations** (UAT, "inherit + small floor"): a task asks what a chat
  would under Confirmations (mode, always allow, always ask), plus one floor - a tool annotated
  destructive still asks in a task when the mode is off. Browser clicks no longer ask with the mode off.
- **"Always allow in tasks"** on a task's approval card: approves the call and adds the tool to
  `tasks.no_approval`, listed (and removable) in Settings → Tasks. Never for a `deny_tools` tool.
- A tool provider that is not set up at all (no mail account, no search engine) offers no tools, is not
  listed as "not available", and shows "not set up" on Status.

### Fixed
- A Gmail clean-up task was declined as "mail is down (no mail account is configured)" although the
  mail is reachable through Outlook on a host: the core's own unconfigured IMAP `mail` was offered and
  named as the need. A need now has to name a namespace that is set up.

## 2026-10-31 — host 2.0.0a32

### Added
- **Meeting slots** ([#11](https://github.com/SikamikanikoBG/Jarvis2/issues/11)): new read-only host tool
  `calendar_free_busy`. Name colleagues (addresses or names) and a window in the chat; Jarvis reads their
  Exchange free/busy plus Arsen's own and proposes 2–3 common slots on weekdays in work hours, earliest first.
  - Tentative counts as busy unless asked otherwise; out of office blocks; "working elsewhere" does not block
    and is named on the slot.
  - Anyone without data (unknown or ambiguous name, no access, error) is named with the reason, never silently
    dropped; for an ambiguous name Jarvis asks which person.
  - Nobody free → the nearest near-misses, each with who blocks it.
  - Arsen picks a slot and gives the agenda; Jarvis books it with `calendar_create` (attendees, invites sent,
    agenda in the body). Chat and voice only, no new screen.

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
