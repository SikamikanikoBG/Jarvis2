# 07 — Alpha report: delegated tasks (issue #10)

**Verdict: GO for UAT** (2026-10-30, core 2.0.0a106 / web alpha.86). No critical or high findings open.
Three passes by a fresh tester who did not see the code; findings in `FINDINGS.md`, screenshots in `C:ProjectsJarvis2-uat-evidencedelegated-tasksshots` (outside the repo).

## Found / fixed / open
- Pass 1: 3 critical, 8 high (incl. F-17, a production bug: one-off reminders 2 h late), 6 medium, 12 low.
- Pass 2: 1 new high (N-1), 3 new medium, 2 new low.
- Fixed by the end: all critical and high; mediums F-11, F-13, F-15, F-16, N-2, N-4; lows L-2, L-4, L-5, F-14, N-6.
- Open (known issues for UAT): N-8 (medium: correct results can read "partly done"), N-1 remainder
  (medium: a table sometimes arrives as prose), F-7 (low: status count), N-7 (low: nav labels squeeze at
  1366 px), L-1, L-3, L-6, L-7, L-9 (low), pre-existing L-10..L-12. F-5 and N-3 rest on tests only.

## Verified
Typed delegation EN + BG (ack ~4–6 s, chat free at once, quick asks inline); honest decline from real
health; evidence-checked "done"; strict approvals (mode off + always_allow, exact args, deny respected,
expiry, survive restart and hard kill); calls (noting, amend, drop, read-back, hang-up and page-drop start
exactly once, call hold, BG call → EN report in home); restart resumes and says so; cancel with partial
work; steer; duplicates; incognito; scheduled runs cannot delegate; flag off mid-flight; mobile/desktop,
dark/light; REST/WS edge cases; regression sweep (chat, calls, normal confirmations, schedules).

Voice TTFT (fake-audio call): no task p50 3.6 s / p90 23.3 s (cold prefix at call start, unrelated to
tasks); task running (paused during the call) p50 3.4 s / p90 3.8 s.

## Not verifiable headless (UAT on Arsen's phone/Brave)
Real web push on Android; heartbeat/drop with a locked screen; a real-microphone English call; paint in
Brave during a running reply; browser-extension tools; mail/Discord approvals.
