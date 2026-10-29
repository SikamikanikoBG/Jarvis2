# 04 — Panel: IT architect — target design for delegated tasks

Status: proposal (discuss-first). Nothing is coded. Baseline main @ aa7128a.
Paths: core = `packages/core/jarvis_core/`, proto = `packages/proto/jarvis_proto/`, web = `web/src/`.
Binding inputs: 01-needs.md, Gate A (02-mockup/DECISION.md: Option A cards in the asking chat;
read back then start after a call; sub-agents only for announced tasks), 03-as-is.md.

## 0. What I checked in 03-as-is (and corrections)

Confirmed against the code: `RunKind`/`ConversationKind` (proto/runs.py:16-23, 150-157); `_BESIDE_A_CALL`
run_engine.py:54, `_PRIORITY` L56-63 (MAIL missing, so `create_run(kind=MAIL)` raises KeyError at L198),
`create_run` L142, no per-conversation serialization in `_dispatch` L323-349 (it only holds non-chat runs
while `on_a_call()` L316), urgency set at L356; `_UNATTENDED` loop.py:94; `needs_confirmation`
settings.py:463-470; `VOICE_STYLE_DEFAULT` settings.py:487-502 with "never 'after the call'" at L494;
settings stored per top-level key (store.py:605-618, so a stored `voice` object keeps the old style text);
`files.attach` → `attachments.add_outgoing` → `take_outgoing(run.id)` bound in `_persist` loop.py:1187-1193;
`engine.confirm(run_id, call_id, …)` L276 is keyed by run id only; the ws handler (api/ws.py) does not check
that the client is subscribed to the run's conversation, so Approve/Deny already works across conversations.

Corrections / additions that matter for this design:
1. **Bus scoping** (engine/bus.py:59-77): `message.created` and `run.updated` are scoped to the message's or
   run's conversation, not sent to everyone. Only `conversation.updated/deleted` and events with no conversation
   (`*.changed`) reach everyone. A new `task.changed` with no conversation field reaches everyone, which is what we want.
2. **`always_ask` is skipped for unattended runs**: `needs_confirmation` returns `not unattended` for always_ask
   (L464-465). Even `*.outlook_send` passes silently in a SCHEDULED run. So task runs must not be in `_UNATTENDED`,
   and `mode="off"` / `always_allow` need a task-specific answer (§8).
3. **Outgoing attachments live only in memory** (`AttachmentStore._outgoing`, attachments.py:124/230). A restart between
   `files.attach` and the final answer loses the binding. This is old debt, and tasks make it more visible (§12).
4. **Rings read the reply by `run_id`** (web store.ts:581). If a report message has no run_id, the ring must carry the
   text in `say`. `CallMe.ring()` already sends `say`.
5. `_CONTEXT_KINDS` (loop.py:95) controls skills, pre-flight and the knowledge learner (L238/244/570). The new kind must be added there.
6. Web `FOLDER_LABELS` is a `Record<FolderKind,…>` (store/selectors.ts:10), so a new ConversationKind breaks
   the TS build until it gets a label. That is useful, because it forces the change.

## 1. Decision: each task gets its own linked conversation (kind `task`), not a child run in the asking chat

There is no per-conversation serialization, so a child run in the asking chat would interleave its tool messages with
Arsen's next turn. It would also pollute that chat's history and its prefix cache, and two runs would write into one
transcript. A task conversation is a real conversation, so it reuses the whole machine unchanged: resume-on-boot,
Run Inspector, compaction, cancel/steer, `files.attach`, idempotency keys, and continuation runs.
Arsen sees the work in the **asking** chat as a card plus a report (Gate A). The task conversation is the
"full result / brief / steps" view the card links to. It sits in a collapsed machine folder **Tasks**, grouped by asking chat
(`folder_key = origin conversation id`, `folder_label = its title`). That is one label in selectors.ts and no new
page. It is left out of the sidebar's attention filter, and never marked unread, because the card is the surface.

## 2. Components

| Component | Where | Role |
|---|---|---|
| `TaskStore` | new `features/tasks.py` (raw SQL, ScheduleStore pattern) | CRUD and atomic state claims on `tasks` |
| `TaskService` | same file; started in `app.start()` before `engine.start()` (like callme, app.py:294) | bus subscriber (`everything`). Maps task-conversation run events to task state, proxies confirmations, writes reports, pushes, runs the call-idle sweeper, reconciles on boot |
| `TasksTools` (builtin namespace `tasks`) | same file | `tasks.delegate`, `tasks.note`, `tasks.list`, `tasks.amend`, `tasks.drop`, `tasks.steer`, `tasks.cancel` (asking side). `task.finish`, `task.ask` (task side, internal namespace `task`) |
| `TasksBlock` | engine/context.py, per-turn, beside VoiceBlock L815 | this conversation's open/noted tasks, plus "finished since your last turn". The work-policy sentence on calls (§9) |
| `CapabilityBlock` | engine/context.py, per-turn | what is down right now (§7) |
| Task framing | `ContextAssembler.system_message`, next to the SCHEDULED block context.py:505 | the stable "you are working a delegated task" block for TASK runs |
| Call registry | in TaskService (memory, rebuilt from `runs.call_id`) | open calls, last voice activity, `call.ended` |
| REST | new `api/tasks.py` | `GET /api/tasks?conversation_id=&open=`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/cancel`, `POST /api/calls/{call_id}/end` (sendBeacon on pagehide) |
| Web | `chat/TaskCard.tsx`, `chat/CallReportCard.tsx`, a Tasks chip in `shell/TopBar.tsx` L57-74, noted list in `voice/CallScreen.tsx` ~L140 | reuses ConfirmCard and PlanChecklist styling, and RunChip for the live step |

Deliberately **not** a new engine. RunEngine, AgentLoop, CallMe, notify and attachments are reused. TaskService is glue
on the bus, the same shape as `features/callme.py`.

## 3. Data model — migration `0017_tasks.sql`

```sql
CREATE TABLE tasks (
  id                     TEXT PRIMARY KEY,                -- "task_…"
  origin_conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  origin_run_id          TEXT,                            -- the run that called tasks.delegate/note
  origin_key             TEXT NOT NULL UNIQUE,            -- "<run_id>:<tool_call_id>" — a resumed run cannot create it twice
  call_id                TEXT,                            -- set when noted on a call (client call session id)
  conversation_id        TEXT UNIQUE REFERENCES conversations(id) ON DELETE SET NULL, -- the task's own (kind task); NULL while noted
  run_id                 TEXT,                            -- current/last run in it (continuations replace it)
  parent_task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  depth                  INTEGER NOT NULL DEFAULT 0,
  title                  TEXT NOT NULL,
  brief                  TEXT NOT NULL,                   -- JSON TaskBrief (§5)
  state                  TEXT NOT NULL,                   -- TaskState (below)
  outcome                TEXT,                            -- JSON TaskOutcome (§6)
  waiting                TEXT,                            -- JSON {run_id, call_id, name, arguments, reason} while a step waits for approval
  report_message_id      TEXT,                            -- set exactly once (§6)
  incognito              INTEGER NOT NULL DEFAULT 0,      -- copied from the origin at creation, never changed
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX tasks_origin ON tasks(origin_conversation_id, state);
CREATE INDEX tasks_call   ON tasks(call_id) WHERE call_id IS NOT NULL;
ALTER TABLE runs     ADD COLUMN call_id TEXT;   -- which client call a voice run belonged to
ALTER TABLE messages ADD COLUMN task_id TEXT;   -- a report (and its anchor) in the asking chat
```

`TaskState`: `noted → queued → running ⇄ waiting (approval) → needs_input | done | partial | not_done | failed | cancelled`,
plus `dropped` (a noted task removed on the call). `queued` vs `running` is read from the run's status, which is the truth
(the same principle as `ConversationActivity`). TaskService writes `running` on `run.started` only so the card has one field to show.

Exactly-once keys:
- **Creation**: `origin_key UNIQUE`. The builtin tool is also non-read-only, so the loop's `claim_idempotency` covers a resumed run as well.
- **Start after a call**: `UPDATE tasks SET state='queued', started_at=? WHERE call_id=? AND state='noted' RETURNING id`.
  This is one statement on the single SQLite writer. A second `call.ended`, a reconnect or the idle sweeper finds nothing to claim.
- **One run per start**: the task conversation and run are created inside the claim's follow-up for the returned ids only.
  `conversation_id UNIQUE`.
- **Report**: `UPDATE tasks SET report_message_id=? WHERE id=? AND report_message_id IS NULL`. Whoever wins writes the message, and boot reconciliation retries.
- Store code: `create_run` INSERT (store.py:512-535) gains `call_id`. `save_run` (L541-559) does not, because call_id never changes.
- `delete_conversation` (features/expiry.py:82) also deletes the origin's task conversations (rows cascade, files do not).

## 4. Proto, events, client messages

proto/runs.py:
- `RunKind.TASK = "task"`, `ConversationKind.TASK = "task"`. `create_run` L164 then derives the conversation kind by itself.
- `Run.call_id: str | None = None`.
- New `proto/tasks.py`: `TaskState`, `TaskBrief{goal, context, constraints[], dod[], approvals[], needs[], deliver_as, language_for_others?}`,
  `DodResult{item, met: bool|None, evidence}`, `TaskOutcome{summary, dod[DodResult], waiting_on?, question?, stopped_reason?}`,
  `Task{…columns…, live: RunStatus|None}`.
- `Message.task_id: str | None`.

proto/events.py:
- `TaskChanged{type:"task.changed", task: Task}`. It carries no conversation, so the bus sends it to every client (header chip,
  cards in any open chat, the call screen). Add it to `ServerEvent` (L294).
- `TaskRemoved{type:"task.removed", task_id}` for dropped tasks and deleted conversations.
- Client: `RunCreateRequest.call_id` and `RunSteerRequest.call_id` (optional), `CallEnded{type:"call.ended", call_id, conversation_id, how:"hangup"|"drop"}`,
  `TaskCancelRequest{type:"task.cancel", task_id}`. Add these to `ClientMessage` (L431).
- **No new confirm message.** The card's Approve/Deny sends the existing `tool.confirm{run_id, call_id}` (events.py:401). The engine resolves it
  by run id whichever conversation the run lives in. The "confirm proxy" is only TaskService copying
  `ToolConfirmRequested`/`RunWaitingUser` from the task conversation into `tasks.waiting`, plus a `task.changed`.
- Web: hand-mirror these in `protocol/types.ts`: the RunKind union L58, `RUN_KINDS` L229, ConversationKind L144, Task types, and the
  reducer cases (with tests in reducer.test.ts).

## 5. How a task is created and run

**Asking side (CHAT runs only)**:
- `tasks.delegate(title, goal, context, constraints, dod[], approvals[], needs[])` starts now.
- `tasks.note(…)` (same arguments) is for voice runs. It parks the task as `noted` under `run.call_id`.
- The brief is the tool arguments. The asking model writes it while it still has the whole chat in front of it, so there is no
  separate summariser call. TaskStore stores it as JSON.
- The task conversation is created with `kind=TASK, folder_key=origin, incognito=origin.incognito`. Its first user message is the
  brief rendered as text. It is English to Jarvis, and `language_for_others` covers drafts addressed to other people.
- Attachment ids the asking chat holds and the brief names are copied by reference into `context`. This is a text list of names and ids
  in v1, readable via the existing attachment tools. No re-binding.
- Run: `create_run(kind=TASK, conversation_id=task_conv, channel=TEXT, call_me=False)`.

**Wiring of RunKind.TASK**:
- `_PRIORITY[TASK] = 2` (after chat and collab, level with meeting). Also add MAIL while there (debt).
- Not in `_BESIDE_A_CALL`, so no task starts while a call is open (§9). The urgency at L356 resolves to BACKGROUND, so on the
  EndpointGate (models/base.py) voice and typed calls go ahead of it.
- `_default_routing[TASK] = CHAT` lane, because tasks read a lot, the same reasoning as SCHEDULED. It is editable in run_routing.
- `_default_budgets[TASK] = RunBudget(max_steps=60, max_tokens=800_000, max_seconds=1800)`, editable. EC-13 budget exhaustion already
  ends in `_finish`/`_wrap_up` (loop.py:1097-1138).
- `_CONTEXT_KINDS += TASK` (skills, pre-flight, KG learner, all incognito-gated already).
- **Not** in `_UNATTENDED` (§8).
- `call_me_default` is untouched. Tasks push through TaskService instead (§6).
- Journal `_SKIPPED_KINDS += TASK`. Sessions `ADDRESSABLE_KINDS` already excludes it, so a task is not a session.
- `_after_run` (run_engine.py:395): no unread flag for TASK conversations.
- **Concurrency cap**: `settings.tasks.max_running` (default 1, see open question 5). `_dispatch` holds a TASK run when that many are active,
  using the same `held` mechanism as call gating. With `JARVIS_MAX_RUNS=3` there is always a slot for Arsen's chat. EC-6: the card shows
  "queued", and the ack can say so because `tasks.delegate` returns the queue position.

**Task side**:
- The stable system block for TASK runs (beside context.py:505) says:
  - you are working a task Arsen delegated, and nobody is watching live;
  - work from the brief;
  - anything outward or irreversible will pause for his approval by itself;
  - if you need something only he knows, call `task.ask(question)`;
  - end with `task.finish(summary, dod[{item, met, evidence}], waiting_on?)`;
  - report to Arsen in English, and keep content for other people in their language.
- Tool exposure is checked in code, not only in the prompt. `_exposed_tools` (loop.py:195) gains the run kind:
  - `task.*` is offered only to TASK runs.
  - `tasks.delegate/note` only to CHAT runs at `depth < settings.tasks.max_depth`.
  - `sessions.say` and `jarvis.call_me` are never offered to TASK runs.
  - `_execute_one` repeats the check, as it already does for memory writers in incognito (L760-765), because a model can name a tool it was not offered.
- `task.ask` writes `outcome.question`, sets `needs_input`, and ends the run normally. Arsen's answer comes from the card's reply box or from the
  asking chat model via `tasks.steer`. It becomes a **continuation run** in the same task conversation, with full history, and `tasks.run_id`
  moves to it. The same path covers EC-14 (a steer to a finished or parked task). A steer to a running task is `engine.steer(run_id, text)` (run_engine.py:244).

**Depth (EC-17)**: `settings.tasks.max_depth = 1` by default, meaning a task cannot delegate. `depth` and `parent_task_id` are stored so the
limit can be raised without a migration.

## 6. Outcome, report, files, push

- **Honest outcome in code.** TaskService computes the state from what happened, not from the prose:
  - `task.finish` with every DoD item `met=true` and non-empty evidence → `done`.
  - Some items met → `partial`.
  - None met → `not_done`.
  - The run ended without `task.finish` (budget, supervisor stop, `_wrap_up` has no tools) → `not_done`, with `stopped_reason` and DoD items `met=null` ("not checked").
  - `run.failed` → `failed`. `run.cancelled` → `cancelled`, with the partial answer (EC-12).
  - "Done" can only come from an explicit per-item claim with evidence. v1 adds no second judge pass (see §15).
- **Report message in the asking chat**: an assistant message with `task_id`, `run_id=NULL`, and content =
  `outcome.summary` + the DoD lines + "full result" (a link to the task conversation). It is short by construction (EC-10). The long final answer stays in the task conversation.
  - It is preceded by a hidden anchor user message `name="task"` (`[task <title> finished]`). Later turns in the asking chat then see a user→assistant pair, and the model knows where the report came from.
  - The web renders the anchor as the card's position, not as a bubble.
- **When it is posted**: only while the asking conversation has no working run (`engine.working_run_in`, run_engine.py:295). Otherwise TaskService
  waits for that run's `_after_run` via `engine.on_finished` (chain it with app._run_finished app.py:198, today a single hook). Inserting
  mid-run would land in that run's history.
  - On a call, the next voice turn's `TasksBlock` lists "finished since your last turn", and the model mentions it at its own pause (EC-8).
  - Nothing interrupts speech.
- **Files**: `files.attach` inside a task run binds to the task's final answer (loop.py:1187-1193), as today. When the report is written,
  TaskService re-binds those attachment ids to the report message (`attachments.bind(ids, message_id=report, conversation_id=origin)`,
  attachments.py:308). Privacy carries over because both conversations share the incognito flag. The files then land "where it was asked" (EC-16).
- **After-call report**: the per-task reports are still written. The web groups the cards that share a `call_id` into one CallReportCard (Gate A).
  The push for a call group goes out once, when every task in it has left `queued/running`.
- **Push**: via `CallMe.ring()` (callme.py:125), with a new entry point `ring_task(conversation_id=origin, kind, say)`.
  - Answering opens a call **in the asking chat** that reads the summary. `say` is in the payload because the report has no run_id.
  - `waiting`/`needs_input` → "Jarvis needs you" at once.
  - done/failed are batched by the existing 4 s gather.
  - The default-ring rule stays: no ring while Arsen is viewing the asking chat (`bus.has_viewers`).
  - Incognito: title only, no summary text in the push body.

## 7. Honesty check (US-5): real signals plus the model's judgement, no word lists

- **CapabilityBlock** (per-turn, CHAT runs including voice, only when `tasks.enabled`) lists **only what is not OK right now**:
  - from `registry.provider_health()` (registry.py:184): name, the error, and since when (TaskService keeps the first time it saw each error);
  - model endpoints whose last probe failed (cached, never probed on the hot path).
  - When everything is up, the block is empty and costs nothing. When the set of down providers does not change, the
    `context_blocks` dedup (story 12) sends a one-line reference.
- **`tasks.delegate` re-checks `needs[]`**, the tool namespaces the model says the task requires. If one is down, it returns a failure
  phrased like `cannot_route` (L209): "workocholic is not reachable since 08:10; tell Arsen you cannot do this now and why, or
  ask if he wants it when it is back". The model then declines or asks. No task row is created, which honours "Not possible → creates no task".
- **"Knows enough / understands the goal"** is the model's judgement, guided by the `tasks.delegate` description: the DoD must be checkable,
  and if a needed fact is missing, ask the one question now (on a call: now, or note it with the question). No classifier, no keyword lists.
- **Inline vs delegate** is likewise the model's call from the tool description ("work that will take many tool steps or minutes;
  quick lookups you answer yourself"). No step-count heuristic in code.

## 8. Confirmation for task runs (US-7) instead of the silent pass

- TASK is **not** in `_UNATTENDED`. Destructive and `always_ask` tools therefore go through `_wait_for_confirmation` (loop.py:960).
  The run parks as `waiting_user`, which holds no slot (L384-386). TaskService mirrors the request onto the card and pushes.
  Approve/Deny uses the existing `tool.confirm`.
- **Settings override for tasks**: `settings.tasks.confirm = "destructive"` (the default) is applied **even if** `confirmations.mode == "off"`.
  `always_allow` is **ignored** for TASK runs unless `settings.tasks.honour_always_allow = true`.
  - Implementation: `needs_confirmation(name, destructive, unattended, strict=run.kind is TASK)`.
  - Reason: an `always_allow: ["workocholic.shell_run"]` entry is exactly the kind of trust that must not carry into a run nobody is watching (the 2026-10-15 reboot).
- Mail policy (unapproved recipient → draft, loop.py:799-807) is unchanged and still applies.
- A resumed mutating call ("did it complete?", loop.py:734-750) goes through the same proxy. The card shows that reason verbatim.
- A cross-conversation confirm needs no new authorization model. It uses the same authenticated WS. A `tool.confirm` for a run that is no
  longer waiting returns False, as today.

## 9. Call lifecycle (US-4, EC-3) and the voice-style conflict

- **Call id**: `web/voice/session.ts` makes a `callId` (uuid) at `start()`. Every `run.create`/`run.steer` from the call carries it, and it is stored on
  `Run.call_id`.
- **Call registry**: TaskService registers the call on its first voice run (`{call_id: (conversation_id, last_voice_at)}`). From then until it closes,
  `on_a_call()` also counts an open call, not only a voice run executing (run_engine.py:316). Task runs therefore wait the whole call out instead of
  starting in the gaps between turns ("voice first"). Runs already executing continue at BACKGROUND urgency.
- **End**:
  - `endCall()` (store.ts:552) sends `call.ended{how:"hangup"}`.
  - `pagehide`/`beforeunload` sends `navigator.sendBeacon('/api/calls/<id>/end')`.
  - **Idle fallback** for a drop or a dead phone: a TaskService sweeper (30 s) closes a call with no voice run executing and
    `last_voice_at` older than `settings.tasks.call_idle_s` (default 180).
  - All three end in the same atomic claim (§3), so the tasks start exactly once whichever arrives first. Rebuild on boot: voice runs with
    `call_id` inside the idle window, plus noted tasks.
- **Read-back (Gate A)**: while noted tasks exist, the hang-up button's first press:
  - shows "Starting 3 tasks";
  - plays one sentence built by the core from the titles, returned by `call.ended` (ack) and spoken with the existing server TTS: "I have three: Gyuzelev reply, Vienna hotels, Q3 slides. Starting now, I'll report back.";
  - then ends.
  - A second press ends at once.
  - It is deterministic text from data, so no extra model call and no wait. A dropped call gets no read-back, so the "Started 3 tasks from the call" push says it instead.
  - During the call, "what have you got?" is answered from `TasksBlock` (noted list), with no tool round trip. Amend and drop go through `tasks.amend/drop`.
- **Voice-style conflict**:
  - Remove the work-policy sentence from `VOICE_STYLE_DEFAULT` (L494: "You have all your tools on the call: do what he asks now, never 'after the call'"),
    so that the style is about speech only.
  - The policy moves into `TasksBlock` on voice runs:
    - `tasks.enabled`: "quick things you do now; work that needs many steps you note with tasks.note and say 'noted' in one sentence".
    - disabled: the old sentence, verbatim. The flag then restores old behaviour fully.
  - **Settings upgrade at load**, in Python (`app.start` after `load_settings`, app.py:285), not SQL: if the stored `voice.style` contains that exact old
    sentence, it is removed and the `voice` key saved once. Arsen's own edits survive. If he edited that sentence itself, nothing changes and a warning is logged.
    This is a one-time migration of our own text, not a filter.

## 10. Incognito (EC-9)

The task conversation is created incognito when the origin is. Everything existing then applies:
- no KG learning (loop.py:570);
- memory-writer tools hidden;
- attachments held off disk;
- journal skip (journal.py:156);
- not a session;
- not searchable.

`tasks.incognito` copies the flag for the push rule (§6). `TasksBlock` and the Tasks folder show an incognito task only inside its own chat.
Delete and expiry of the origin delete the task conversations.

## 11. Concurrency, GPU, restart

- Voice first: an open call holds new TASK starts (§9). EndpointGate keeps voice ahead. `tasks.max_running` caps background load.
- Restart (EC-4):
  - Task runs resume through the existing `_resume_on_boot` (run_engine.py:128).
  - TaskService `reconcile()` on start:
    - (a) a terminal run whose task has no report → write the report;
    - (b) noted tasks of a call that is already idle → start them;
    - (c) tasks in `waiting` → re-mirror from the run's last `ToolConfirmRequested`.
  - If a task run cannot resume (interrupted with no run), it becomes `failed` with "interrupted by restart" and is reported. It is never silently lost.
- Known gap: outgoing files attached before a restart but not yet bound are lost (attachments.py:124). Fix it in this feature by persisting
  `run_id` on the attachment row (it already has `conversation_id`) and re-reading it in `take_outgoing`. That is a small change, and tasks are where long runs meet restarts.

## 12. Observability, security

- Events: `task.changed`/`task.removed`. All run events stay in the task conversation, so Run Inspector works there unchanged. Log lines are
  `task <id> <from>→<to>`.
- `/api/status` (api/rest.py:39-64) gains `"tasks": {noted, queued, running, waiting, needs_input}` and `"calls_open": n`.
- Security:
  - no new auth surface;
  - `tasks.*` is never offered to COLLAB (an outside caller over `/mcp`), SCHEDULED, TRIAGE, MEETING or SYSTEM runs. Only a run Arsen is present for can create a task, which is "only announced tasks" enforced by kind;
  - a task's tool results (web pages) can contain injected instructions. Every outward action still stops for approval (§8);
  - REST `POST /api/tasks/{id}/cancel` → `engine.cancel(run_id)`, or a state change for `noted`/`needs_input`.

## 13. Rollout / rollback

- `settings.tasks` (new top-level key, so stored separately):
  - `enabled` (default **false** on first deploy, switched on for alpha/UAT);
  - `max_running = 1`, `max_depth = 1`, `call_idle_s = 180`;
  - `confirm = "destructive"`, `honour_always_allow = false`.
- With `enabled=false`: no tools, no blocks, the old voice sentence, and the call messages accepted but ignored.
- Migration 0017 is additive (a new table and nullable columns), so a code revert is safe and the columns sit unused.
- Order:
  1. Proto plus migration.
  2. Engine wiring (kind, priority, routing, budget, cap, confirm strictness).
  3. TaskService plus tools plus blocks, with FakeTurn tests.
  4. Call lifecycle.
  5. Web.
  6. Alpha on :9121.
  7. Flag on in prod.
- Each step: tested → committed → pushed → `bash scripts/deploy_ardi.sh`. Version bumps in core, proto and web, plus the `sw.js` SHELL bump if a notice type changes.
- Tests (Harness + FakeTurn):
  - delegate → card → finish → report in origin with files;
  - finish missing → `not_done`;
  - confirm proxied across conversations;
  - mode=off and always_allow still ask in TASK;
  - `call.ended` ×2 plus the sweeper → one start;
  - restart mid-task → resume and report once;
  - report deferred while the origin has a working run;
  - incognito propagation;
  - depth and kind exposure refused in `_execute_one`;
  - the voice-style upgrade keeps custom text;
  - the API contract snapshot.

## 14. What I would deliberately NOT build (v1)

- No Tasks page or sidebar entry beyond the machine folder (Option B rejected at Gate A).
- No outside providers (Claude Code or cloud) and no recurring tasks (schedules cover that).
- No second "judge" verification of DoD claims. Add it only if false "done" is observed. The per-item evidence is visible on the card.
- No "wait until the host is back" scheduling (EC-5). Decline honestly; Arsen can ask again or ask for a one-shot schedule.
- No fuzzy dedup of "the same ask twice" in code. The open-task list in `TasksBlock` plus the tool description lets the model amend instead of duplicate (EC-2).
  Double sends are covered by `origin_key`.
- No model-written read-back at hang-up (deterministic sentence instead), and no live step ticker beyond what RunChip already shows.
- No generated TS types (keep the hand mirror; generating them is a separate cleanup).
- No change to `sessions.say` or its 2026-09-19 rule.

## 15. Open questions for Arsen / the panel

1. **Approval strictness**: OK that task runs ask for destructive actions even with `confirmations.mode="off"`, and ignore `always_allow` (e.g. shell on workocholic)?
2. **Calls hold tasks**: while a call is open, no new task run starts, including tasks delegated from a typed chat at the same moment. Acceptable, or only hold during a voice run (today's behaviour)?
3. **Idle fallback** of 180 s: a silent open call longer than that would start the noted tasks early. Is that fine, or do we want a client heartbeat to tell a silent call from a dropped one?
4. **Push for a finished task**: a ring (Answer reads the summary on a call) as in story 11, or a quieter notice? Waiting-for-approval always rings.
5. **`max_running` default**: 1 (safest on the shared 3090 pair) or 2?
6. **Task conversations visible** in a collapsed "Tasks" machine folder (inspectable, can be talked to), or fully hidden and reachable only from the card?
7. **Read-back** as a fixed sentence built from titles (instant), or does Arsen want Jarvis's own words (one more model call, ~5 s before hang-up)?
