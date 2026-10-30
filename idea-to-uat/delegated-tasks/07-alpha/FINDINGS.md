# 07 — Alpha findings (pass 1, 2026-10-30, alpha core 2.0.0a102 on ardi :9121)

Verdict pass 1: **NO-GO** — 3 critical, 7 high (+1 high pre-existing). Safety holds; the core promise
(delegate, free the chat, honest "done") does not. Screenshots: `07-alpha/shots/`. Tester notes:
scratchpad `findings-notes.txt`.

## Critical
- **F-1 (US-1) Jarvis never delegates by himself.** 5 broad research asks (EN+BG, incl. "get back to me
  when you're done") all ran inline 51 s–2 min 22 s. The planner writes a "Use web.search…" plan ~1.5 s
  in, before the model decides. Convs `conv_1a18bdd2bb4_e5b3dca6`, `conv_1a18be30bcd_39b1f308`,
  `conv_1a18be1002f_ea9afcc0`.
- **F-2 (US-1) After delegating, the chat keeps working.** Sometimes also does the research inline (Kapana
  ack at 93 s, `conv_1a18be5b09e_595aece3`); otherwise blocks on `jarvis.wait_until(tasks.list…, 900 s)`
  because the planner's step says "wait for the background task" — dentist chat blocked 481 s then
  posted a duplicate "Done" next to the report (`conv_1a18bedee5f_6ba8dc58`); e-reader chat never acked.
- **F-3 (US-4) On a call, "after this call, please…" is done during the call** (web search/fetch on the
  line, nothing noted; `conv_1a18be9c752_bc8c748f`). Works only with a literal "запиши си задача"; then
  note/amend/read-back/drop/noted list/start-on-hang-up/after-call report all work.

## High
- **F-4 (US-3) "Done" is unreachable.** Model cites filenames/object ids/nothing; tool results never show
  the call/attachment id the check needs. A real attachment (`att_1a18c27bdc3…`) still "claimed, not
  verified" (`task_1a18c27738c_c81ff107`). Dentist cited another chat's schedule id (rightly rejected).
- **F-5 (US-3, EC-13) Supervisor-stopped task = "not done" even with a complete answer**; empty summary,
  every item "not reported", report cut "… (the rest is in the task's own log)", supervisor text shown
  twice, once the model's "now let me fetch…" thinking. Kapana, sushi, moussaka.
- **F-6 (EC-4) Restart mid-task → "cancelled", "Stopped: cancelled by Arsen"** (untrue), items wiped
  (`task_1a18bf6a891_1bcba945`).
- **F-7 (EC-6) Two tasks ran at once**: a task paused on approval freed its slot; after Deny it resumed
  while another ran (`/api/tasks/status` running:2).
- **F-8 (US-1) Ack takes 30–150 s** (71 s thinking, 45 s brief writing, 80-char title limit failure +
  retry, planner checking steps).
- **F-9 (EC-2) Same ask twice → two tasks** (`conv_1a18c07f31e_034622c2`); both acks said "running" while queued.
- **F-10 (US-5) Honesty fails**: no laptop host on alpha, still "I'll read the file from the laptop"
  (`conv_1a18bf37aa9_60a0b924`); "book a table" searched 144 s before declining; web search returning
  nothing never surfaced as unavailable.
- **F-17 (pre-existing, schedules) One-off `at` reminders fire 2 h late**: `next_fire` stored with `+02:00`,
  due check compares strings against UTC. Production's one-off "Messenger РД" never fired. Delegated
  reminder tasks create exactly these.

## Medium
- **F-11 (US-6)** Tasks chip: desktop hidden under top nav (clicks hit the nav); mobile popover off the left edge.
- **F-12 (EC-14)** Steer just before a run ends → `200 running`, never delivered.
- **F-13** Flag off mid-flight: running task's `task.finish` fails → "not done"; queued task starts without
  task tools; partial nested `PATCH {"tasks":{…}}` resets `enabled` to false.
- **F-14** Ack says "Done — …" for just-started tasks (sidebar preview reads finished); queued never said.
- **F-15 (cold start)** New chat gives no hint that Jarvis can take tasks.
- **F-16 (EC-12)** Cancelled task's report shows no partial work.

## Low
- L-1 No live step / elapsed time on the card. L-2 Long titles one line, full title unreachable.
- L-3 Card and report repeat the same item list. L-4 Queued task shows "started" from creation.
- L-5 Over-long title errors instead of being shortened. L-6 Unknown ids on ws `task.cancel`/`task.steer` fail silently.
- L-7 422 echoes the whole 200k-char input. L-8 Ack exposes internal task ids.
- L-9 After a hard kill a read-only fetch asks for approval again.
- L-10 (pre-existing) "WebSocket is not connected. Need to call accept first" traceback in log.
- L-11 (pre-existing) "[The user cut you off…]" visible in transcript. L-12 (pre-existing) supervisor suggests non-existent tool "web.browse".

## Worked
Approvals (exact args, expiry, approve/deny, deny-then-approve ignored, strict with mode=off+always_allow,
push attempted, survive restart and hard kill); call pause/hold; hang-up exactly once; drop after timeout
(62 s at 60 s); BG call → EN replies; steer from card; incognito; scheduled runs cannot delegate;
"what are you working on?" EN/BG; hidden task convs + "Open work"; report waits for idle chat; attachment
moves to report; REST 401/404/422; normal-chat confirmations unchanged; Settings section.
Voice TTFT (fake-audio path): no task p50 2.1 s / p90 13.1 s (outlier = concurrent brief writing);
task running (paused) p50 2.2 s / p90 2.5 s.

## Environment
- SearXNG on ardi rate-limited (Brave, DDG, Google CSE suspended) — **shared with production**.
- STT hears the alpha's English TTS as Bulgarian → call tests ran in Bulgarian.
- PROJECT.md step 3 omits `language_hint`; copied from production at 08:21 UTC; earlier EN-rule results void.
- Alpha state: confirmations.mode=off + always_allow ["schedule.*"], call_drop_after_s=60, task
  `task_1a18c2b4187_288fe861` running, fake push device, `alpha-pw` container + 9121 tunnel up.

# Pass 2 (2026-10-30, alpha 2.0.0a105 / web alpha.84) — verdict NO-GO pending N-1
Fixed: F-1, F-2, F-3, F-4, F-6, F-8, F-9, F-13, F-15, F-17, L-2, L-4, L-5 (L-8 not seen).
Partly: F-7 (low: status shows running:2 while waiting), F-10 (low: "book a table" ~50 s search),
F-11 (medium: desktop chip hidden under nav at 1366/1024 px), F-14 (low: "Done — handed off" still appears).
Still failing: F-16 (medium: cancelled report has no partial work). Not re-verified: F-5 (stop reason shown twice), F-12.
New:
- N-1 (HIGH, US-3/EC-10) Report in the asking chat describes the result but does not contain it (table stays in hidden task conv); steer of a finished task gives an identical report.
- N-2 (medium) On a call Jarvis states facts from memory (wrong peak) that spread into the DoD and the KG.
- N-3 (medium) One failed web search ends a task that could use fetch ("web is not reachable").
- N-4 (medium, US-7) After Deny, the task requests the identical call again.
- N-5 (low) Outward items satisfied by a no-approval tool (files.attach) can never verify.
- N-6 (low) Explicit "hand this to a background task" for a small job done inline.
Regression sweep clean. TTFT fake-audio: no task p50 3.6 s / p90 23.3 s (cold prefix, likely prod load); task running p50 3.4 / p90 3.8 s.
Not verifiable headless: Android push, locked-screen heartbeat, real-mic English call, Brave paint, extension tools, mail approvals.
GO once N-1 is fixed, with F-11, F-16, N-2, N-3, N-4 as known mediums.
