# Gate A — decision (2026-10-29, Arsen)

- **Layout: Option A — task cards in the chat that asked.** The card updates in place
  (running → done / waiting on you / not done), the report is a Jarvis message under it, a
  "Tasks · N" chip in the chat header lists this chat's tasks, push on finish. Approve/Deny on the
  card for steps that need confirmation.
- **After a call: read back, then start.** Before hang-up Jarvis says the noted list in one
  sentence; at hang-up (or a dropped call) the tasks start exactly once; one after-call report
  card lists per task done / waiting / not done. The call screen shows the noted list live
  (mockup tab "Call").
- **Self-talk rule: only announced tasks.** Jarvis may start a sub-agent only for a task it told
  Arsen about, with a brief + DoD, visible and cancellable. `sessions.say` keeps its 2026-09-19 rule.
- Honesty check (take / ask / decline) as in the mockup tab "Honesty" — applies to all of the above.
