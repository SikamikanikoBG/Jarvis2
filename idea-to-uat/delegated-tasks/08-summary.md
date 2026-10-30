# 08 — Summary: delegated tasks (issue #10)

**Built:** Jarvis hands longer work to a background task (own hidden conversation, brief + Definition
of Done), answers in one line within seconds, and reports back in the asking chat with the result and a
quiet push. On a call he notes asks that take work, reads them back when Arsen wraps up, starts them
exactly once after hang-up or a dropped call, and sends one after-call report. Honest take / ask /
decline from real health. Strict approvals inside tasks; "done" only with code-checked evidence.

**Where:** main @ 126e8fa; production core 2.0.0a106 / proto a37 / web alpha.86 on ardi, healthy,
**`tasks.enabled` off** (Settings → Tasks). Story `docs/stories/13_delegated_tasks.md`, `docs/API.md`,
`CHANGELOG.md`, alpha how-to `idea-to-uat/PROJECT.md`.

**Numbers:** ~75 new tests (core + web). Alpha over 3 passes: 3 critical + 9 high + 9 medium + ~14 low
found; all critical/high fixed; open = N-8, N-1 remainder (medium), F-7, N-7 and a few lows.
Voice TTFT unaffected by a running task (p50 3.4 s / p90 3.8 s). Bonus production fixes: one-off
reminders 2 h late; approved interrupted sends re-run without asking after a restart.

**UAT go/no-go:** GO (`07-alpha/REPORT.md`).

**Left for Arsen (UAT):** switch the flag on; a real call from the phone with the screen locked → noted
task → after-call report + push; look at the card in Brave during a running reply.

**Cleanup:** alpha container + volume removed, Playwright container removed, worktree + branch removed.
Screenshots kept outside the repo: `C:\Projects\Jarvis2-uat-evidence\delegated-tasks\shots`.
Environment note: SearXNG on ardi (shared with production) was rate-limited during the alpha.
