# 04 — Plan: delegated tasks (synthesis of the panel, 2026-10-29)

Panel files: `04-panel/architect.md`, `developer.md`, `devils-advocate.md`, `rebuttal.md`,
`architect-answers.md`, `developer-answers.md`. After one challenge round all three converged;
remaining differences are listed at the end.

## Chosen approach
A **task** is a first-class entity (`tasks` table) that links the chat that asked, its **own linked
conversation** (new kind `task`) and an ordinary engine run of the new `RunKind.TASK`. The run reuses
the existing queue, cancel, steer, resume-on-boot, budgets, supervisor and `_wrap_up`. The asking
chat shows a **task card** (Gate A option A) and later the **report**; the task's work never runs
inside the asking chat (the engine does not serialize runs per conversation, so a child run would
interleave with Arsen's next turn and churn the prefix cache).

Rejected: child run inside the asking chat (interleaving, cache churn); reusing `sessions.say` into a
"Tasks" chat (breaks the 2026-09-19 rule, no DoD/state); schedules "run once" (no card, no report
contract); a separate Tasks page (Gate A); a second judge model for "done" (code-checked evidence
instead); fuzzy duplicate detection in code (model sees open tasks; double sends covered by a DB key).

## Key design rules (agreed by all three)
1. **Safety (US-7)** — task runs are *not* unattended. In a task run every non-read-only tool pauses
   for Arsen's approval, **regardless of `confirmations.mode` (live = "off") and `always_allow`**,
   except a short settings list `tasks.no_approval` (default: navigation + Jarvis's own memory).
   `shell_run`, `fs_write`, `fs_edit` are not offered to task runs at all in v1. Browser tools are
   marked harmless by the extension, so this rule does not rely on annotations. Approvals show the
   exact tool arguments, expire after `tasks.approval_ttl` (4 h); the agent re-reads the source
   before asking again. The push opens the card — no one-tap approve.
2. **Honest "done" (US-3)** — the task ends with `task.finish(summary, dod[{item, met, evidence_refs}])`.
   Code checks each ref exists in *this task's* log (tool call / attachment) and succeeded; an
   outward item needs an approved, successful call. An item without valid refs shows
   **"claimed, not verified"**, and the task cannot be "done" (→ partial). Budget end → `_wrap_up` →
   "not done / stopped here".
3. **Brief fidelity (US-2)** — code stores Arsen's own words verbatim next to the brief; the brief
   (goal, context, constraints, DoD, approvals, needs) has an `assumptions[]` list shown on the card.
4. **Honesty "can I" (US-5)** — a `HealthBlock` in context lists only what is down (from
   `provider_health()`, browser per host counted as a provider); `tasks.delegate` checks the tool
   namespaces the task needs **at creation and again at start**; the first "not reachable" tool
   failure (typed flag, not text matching) ends the task "not done: X down since hh:mm". "Knows
   enough / understands the goal" is the model's judgement, surfaced as a question or assumptions.
   No keyword lists anywhere.
5. **Who may delegate** — only runs started by Arsen's own message (CHAT kind), never after another
   chat steered into them; tasks cannot delegate (`max_depth=1`), get no `sessions.say`/`call_me`.
   Enforced in `_exposed_tools` *and* re-checked at execution.
6. **GPU / voice first** — at most **1 task running** (`tasks.max_running`); no task starts while a
   call is open; a running task **pauses between steps during a call** (paused time not budgeted);
   lane = chat by default, decided by measurement on the alpha. Voice TTFT p50/p90 with and without a
   running task is part of the DoD.
7. **Calls (US-4)** — the web gives each call an id, sends it with each voice turn, a heartbeat every
   20 s and `call.ended` on hang-up. Hang-up ends the call at once; no heartbeat for 120–240 s
   (Chrome throttles timers on a locked phone) → treated as dropped. Silence alone never ends a
   call. Noted tasks start **exactly once** (`UPDATE … WHERE state='noted' RETURNING`). On a call
   `tasks.note` takes only a title + Arsen's words (no brief writing while he waits); the task's first
   step writes the full brief after the call. The live **noted list on the call screen** (his words
   as heard, so misheard names/dates are visible) is the confirmation; Jarvis reads the list back
   when Arsen wraps up.
8. **Voice style conflict** — Arsen's live `voice.style` (edited 2026-10-20) contains "never after the
   call" verbatim. A one-time upgrade removes that one sentence and keeps his other edits; if it
   cannot find it, it logs a warning and Settings shows a hint. The tasks block on calls also says it
   takes precedence.
9. **Report placement** — the report is written into the asking chat only when that chat has no run
   working (no interleaving with a streaming reply); attached files move onto the report; one push.
   Task conversations are skipped by the day journal and never mark anything unread.
10. **Incognito** carries over to the task conversation (journal, KG, sessions excluded).
11. **Rollout** — behind `settings.tasks.enabled` (off by default) until the alpha passes; migrations
    only add (old image still boots); rollback = flag off.

## Work breakdown (ordered, each slice deployable)

**Slice 0 — groundwork (~1 d)**
- `RunKind.TASK` / `ConversationKind.TASK`; `_PRIORITY`, routing, budgets for TASK **and MAIL** (debt);
  settings validator that backfills kinds missing from stored `budgets`/`run_routing`; test that every
  RunKind has priority/lane/budget. Web `RunKind` mirror incl. `'mail'`.

**Slice 1 — typed delegation, card, honest report, safe approvals (~9 d)**
- Migration 0017 `tasks` (+ `messages.task_id`); proto `tasks.py` (Task, TaskBrief, DodResult,
  TaskOutcome, TaskState) + `task.changed`/`task.removed` events + `task.cancel` client message.
- `features/tasks.py`: TaskStore, TaskService (bus listener: state from runs, approval mirror into
  `tasks.waiting`, report writer, push), tools `tasks.delegate/list/cancel/steer` (chat side),
  `task.ask/finish/brief` (task side); TASK system block; `TasksBlock` (open tasks) + `HealthBlock`.
- Strict task confirmations (rule 1), evidence check (rule 2), health checks (rule 4), who-may-delegate
  (rule 5), max_running + call hold + pause between steps (rule 6), journal/unread/incognito.
- Web: `TaskCard` (state, DoD with ✅/❌/"claimed, not verified", assumptions, Brief, Cancel,
  Approve/Deny via existing `tool.confirm`, Answer box for `needs_input`), `Tasks · N` chip in TopBar,
  reducer + transcript item, report under the card.

**Slice 2 — call noting + after-call report (~7 d)**
- 0018 `runs.call_id` (+ calls ledger), client `call_id` on run.create/steer, heartbeat, `call.ended`,
  sweeper; `tasks.note/drop/amend`; exactly-once start; voice style upgrade; after-call report card +
  one push; web call id/heartbeat, `NotedList` on CallScreen.

**Slice 3 — polish (~1 d)**: steer box on a running card (EC-14), restart explanations on the card
(interrupted / "did it complete?"), approval expiry UX.

Total ≈ 18 dev-days of agent work (estimate).

## Test plan (→ stories / edge cases)
- pytest (Harness + FakeTurn): delegate → card → report (US-1/2/3, EC-1, EC-10, EC-16); evidence check
  incl. forged/failed refs (US-3); strict approval incl. `browser.type(submit=true)` pausing with
  mode "off" (US-7); no shell/fs_write offered; who-may-delegate incl. steered chat run (rule 5);
  health at create/start, host dying mid-task (US-5, EC-5); needs_input + answer continuation (EC-7);
  cancel (EC-12); budget wrap-up (EC-13); restart mid-task + approval across restart (EC-4); depth
  (EC-17); incognito (EC-9); max_running/queue (EC-6); call: note/drop, exactly-once start on double
  `call.ended`/reconnect/sweeper (EC-2, EC-3), task finishing during a call (EC-8), English report
  for Bulgarian call (EC-11); voice style upgrade (match / no match / user edits kept).
- Regression: sessions (hops, collab), schedules, triage/meeting unattended unchanged, normal-chat
  confirmations unchanged, call-me, journal, conversation delete cascade, `test_api_contract`.
- Web vitest: reducer `task.changed/removed`, transcript placement of cards/reports, call heartbeat.
- Alpha (throw-away `jarvis2-core:alpha` on ardi :9121, real model, read-only real tools): typed and
  headless voice (fake WAV) flows, one real browser research task judged on quality, one approval
  flow, Bulgarian + English asks, mobile + desktop, dark mode, voice TTFT p50/p90 with/without a task.
- Live (after flag on): a real call from Arsen's phone with the screen locked that produces a noted
  task and an after-call report; card checked in his Brave during a running reply.

## Risks → mitigations
- 27B model delegates badly / writes weak briefs → assumptions on the card, verbatim words, alpha
  judges real tasks on quality; flag stays off until it passes.
- False "done" → code-checked evidence; "claimed, not verified" visible.
- Outward action without approval → strict task confirmations + no shell/fs_write + tests.
- Voice latency → 1 task, call hold, pause between steps, measured TTFT in DoD.
- Android drops the page silently → heartbeat sweeper; real-phone test in DoD.
- Push spam → one push per task finish / per after-call report; approvals one pending push per task.
- Hand-mirrored TS types drift → contract test that types.ts covers proto events.
- Shared working tree with other sessions → stage own files only; deploy from clean main.

## Remaining differences (for Arsen)
- **Read-back at hang-up:** Gate A said "read back, then start". Panel now: the noted list on the call
  screen is the confirmation and Jarvis reads it back when you wrap up (model judgement), so the red
  button only hangs up. Alternative: a fixed sentence from titles at hang-up (instant but changes what
  the button does).
- **Finished-task push:** a ring (Answer reads the summary, story 11) or a quiet notice? Approvals
  always notify.
- **Task conversations:** visible in a collapsed "Tasks" folder in the sidebar, or reachable only
  from the card?
- **Dropped-call timeout:** 120 s (architect) vs 240 s (developer, locked-phone throttling).

## Gate B — decisions (2026-10-29, Arsen): "all approved, proceed all phases"
- Plan approved as written (slices 0, 1, 2, 3; flag off until alpha passes).
- Read-back: the call screen shows the noted list live (his words as heard); Jarvis reads it back when
  he wraps up; the red button only hangs up.
- Finished-task push: quiet notice that opens the card (approvals always notify).
- Task conversations: hidden from the sidebar, reachable only from the card.
- Dropped-call timeout: setting `tasks.call_drop_after_s`, default 240 s (locked-phone timer throttling).
