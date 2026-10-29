# 04 — Panel: devil's advocate rebuttal (challenge round, 2026-10-29)

References: **A** = architect.md, **D** = developer.md. Line numbers are those files' lines.
Concern ids refer to devils-advocate.md.

## Resolution table

| My concern | Architect | Developer |
|---|---|---|
| **C1** Outward actions not gated (browser.*, always_allow, mode=off, shell) | **partly.** A:226-228 makes mode=off and always_allow strict for TASK. But browser.* stays non-destructive, and A:300 ("every outward action still stops") is false for it. Shell stays exposed. | **no.** D:205-207 says "confirmations already pause". That is only true for destructiveHint tools. always_allow and mode=off are honoured. |
| **C2** "Done" is self-reported | **partly.** A:178-184 requires a per-item claim plus non-empty evidence. The evidence is free text the model writes about itself, and A:337 defers any check. | **no.** D:125-128: done = every item reported done via `tasks.report`. DodItem evidence is optional (D:48). |
| **C3** Brief misread / context lost | **no.** A:132: the brief is the model's tool arguments. No verbatim ask, no assumptions. | **no.** Same, D:137. |
| **H1** GPU contention / voice latency | **partly.** max_running=1 (A:152). A whole open call holds new starts (A:240). But a task already running keeps going during the call (A:241), and there is no TTFT measurement. | **no.** D:59 puts TASK on the BACKGROUND lane (4 min a step in the newsletter case, shared with triage). D:61 has max_running=2, against its own risk note D:338. Tasks start between call turns (only `_BESIDE_A_CALL` gating). |
| **H2** "Can I" from code, not the model | **partly.** CapabilityBlock plus a `needs[]` re-check (A:208-215). But only at delegate time, not when a noted task actually starts. `needs[]` is model-declared. | **partly.** Deferred to slice 3 (D:217), so slices 1-2 ship without it. |
| **H3** Voice: note cost, end detection, read-back, voice-style | **partly.** sendBeacon plus a 180 s *silence* sweeper (A:245). The read-back is built from titles (A:251). `tasks.note` takes the full brief on the call (A:131). | **partly.** Heartbeat `call.alive` (D:61, 183) is better. The voice-style swap is "only when equal to an old default" (D:68-72). `tasks.note` also takes the full brief on the call. |
| **H4** Approvals: exact args, expiry, stale context | **partly.** Uses ConfirmCard and pushes at once. No expiry and no re-read. | **partly.** One-line action in slice 4 (D:226). No expiry. |
| **H5** Restarts | **yes.** A:282-290 reconcile plus the attachment fix. | **yes.** D:119-121 and D:134, plus the S1 restart test. |
| **M1** Report lands mid-run | **yes.** Waits for the origin's run to end (A:189). | **no.** Writes at once. D:343-346 accepts that the report lands mid-flight. |
| **M2** Noise | **yes.** Journal skip (A:150), one push per call group (A:198). | **no.** Task conversations stay in the overview (D:165), which churns TodayBlock on every call turn. The parent is also marked unread. |

## Challenges that still need an answer

1. **The browser is the hole: both docs claim a gate that doesn't exist for it.**
   - Approval in both docs needs `destructive`. `browser.click`, `browser.type submit=true` and the page-eval tool are `destructive:false`, and they run in Arsen's logged-in Brave. A task sub-agent can book, pay or post with no pause.
   - D also honours `always_allow` and `mode=off` in tasks.
   - Neither removes `shell_run` from task runs. The 2026-10-11 outage was a long job running unattended on jarvisvm.

   **Accept if:**
   - TASK runs pause on every non-read-only call to a tool marked `openWorldHint`. This needs a one-line annotation change in the extension, so browser.click/type/eval are openWorld.
   - Adopt A's strict rule (A:228), not D's.
   - `shell_run` and fs_write are not exposed to TASK in v1, set per kind in settings.
   - Add a test: a task that does `browser.type(submit=true)` ends `waiting_user`.

2. **Evidence that nobody checks is not evidence.**
   - A's "non-empty evidence" and D's `tasks.report` are both the 27B grading itself. That is the "it's fixed" pattern.
   - A:337 says "add a judge only if false done is observed". The only way it gets observed is Arsen being burned.

   **Accept if:**
   - Each DoD item's evidence is a **tool-call id** (or attachment id / fetched URL) from this task's ledger.
   - TaskService checks that the id exists, belongs to this task and its result was not `isError`. Otherwise the item shows **"claimed, not verified"**, never ✅, and the task can't be `done`.
   - For outward items, "sent to Y" counts as done only if a successful send to Y is in the ledger.
   - No second model pass is needed for v1.

3. **The brief is written from memory, and on a call it is written while Arsen waits.**
   - Both docs make the brief the model's tool arguments. Nothing preserves what Arsen actually said.
   - Both also have `tasks.note` take the full brief on a voice turn: a long tool argument generated in silence.

   **Accept if:**
   - The brief stores Arsen's verbatim utterance(s), in the original language, next to the paraphrase.
   - It carries an `assumptions[]` field shown on the card.
   - On a call, `tasks.note` takes only `{title, verbatim}`. The full brief is written after the call ends, from the call transcript, before the run starts.
   - The worker may `sessions.read` its origin (read-only).
   - Measure: the voice-turn latency of a "noted" turn against a normal turn, with the number in the issue.

4. **GPU: pick A's gating, fix D's defaults, and measure.**
   - D's choices (BACKGROUND lane, max_running=2, starts between call turns) put voice back behind long prefills. On the background lane they also slow tasks to minutes a step.
   - A's call-hold covers starts only. A task already mid-run when Arsen calls keeps prefilling 60-100k tokens beside his voice turns.

   **Accept if:**
   - Use A's whole-call hold and max_running=1.
   - Routing goes to whichever lane measures better, decided on the alpha.
   - While a call is open, a running TASK's *next model step* waits (park it between steps; the steer/control check at the top of a step already exists).
   - DoD: voice TTFT p50/p90 measured on the live box, with a call idle vs a call with one task running.

5. **Call end: silence is not a drop, and the read-back must carry the slots.**
   - A ends a call after 180 s with no voice run. Arsen listening, thinking or driving quietly for 3 minutes is not a dropped call, and tasks would start mid-call.
   - D's heartbeat is right, but nobody has shown it survives Android Chrome with the screen locked or the ear-pose black screen (timers get throttled).
   - A's read-back is built from titles, on the *first* press of hang-up. That changes the red button's meaning, a double press skips it, and an English title hides a misheard name or date.

   **Accept if:**
   - The heartbeat (D) drives the sweeper. The idle threshold is larger than the throttled timer interval.
   - It is tested on Arsen's real phone with the screen locked for 3 minutes. Headless can't prove it.
   - The read-back is not on the hang-up button. The noted list on the call screen is the confirmation.
   - "What have you got?" and the end-of-call sentence include the key slots (who / when / how much) as heard.

6. **The voice-style migration will silently not happen on the live box.**
   - D swaps the style only when the stored text equals an old default. A removes it only when the exact sentence is present.
   - If the live `voice.style` was ever edited, or stored from an older default (story 12 edited the default), both are no-ops. Then the live call still says "never after the call", while the alpha, with fresh settings, passes. That is the mock-passed / live-failed pattern.

   **Accept if:**
   - The plan quotes the **current live stored `voice.style`** now, before coding.
   - The task policy lives in TasksBlock and states that it takes precedence over the style's "do it now" line.
   - A no-op upgrade logs a warning and shows a Settings hint.
   - The live check is a real call that produces a "noted" task.

7. **"Can I" must be re-checked at start, and must not depend on the model listing its needs.**
   - `needs[]` is declared by the model: omit `browser`, and the check passes.
   - A noted task can start 20+ minutes later, when the host or Brave has gone down.
   - D ships no health check until slice 3.

   **Accept if:**
   - The health check moves to slice 1, using A's CapabilityBlock.
   - It runs again at task start: an unreachable provider gives an honest "not started: X is down since hh:mm" card, never a silent failure mid-run.
   - Extension connectivity (Brave on jarvisvm) counts as a provider.
   - A worker's first failed call to a down provider ends the task as `not_done` with that reason. It does not wrap up into a vague summary.

8. **Who may delegate, and what an approval means hours later.**
   - Both docs allow `tasks.delegate` from any CHAT run. But `sessions.say` (`_deliver`) creates CHAT runs in other chats, so a scheduled or other-session chain can spawn a task nobody announced to Arsen.
   - Approvals have no expiry. A draft approved from the lock screen 4 hours later may answer a thread that has moved on.

   **Accept if:**
   - Delegation is allowed only in runs started by Arsen's own client message (ws `run.create` from the owner), checked in code.
   - A pending approval shows the exact tool arguments (recipient, amount, URL, first lines).
   - It expires after a setting. On expiry, re-reading the source (thread or page) is required before re-asking.
   - Approve opens the card, not a one-tap approve in the notification.

**Smaller items:**
- Use A's report placement (wait for the origin run to end) and A's journal skip over D's (M1, M2).
- Ship with `tasks.enabled=false` until the alpha passes (A:306, not D:61).
- D's alpha with "no MCP" (D:294) can't exercise the browser or approval gate, which are the risky parts. Run at least one real research task with read-only real tools, and judge it on quality.
- A's hidden anchor `name="task"` user message (A:187): make sure the model can't read it as Arsen speaking.
