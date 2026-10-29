# 04 — Panel: DEVELOPER (delegated tasks, Gate A = Option A)

Read against main @ aa7128a. Paths: core = `packages/core/jarvis_core/`, proto = `packages/proto/jarvis_proto/`,
web = `web/src/`. Line numbers are today's.

## 0. The shape in one paragraph

A **Task** is a row in its own table that ties together three things: the chat that asked (`parent_conversation_id`),
a hidden **work conversation** (new `ConversationKind.TASK`) where a normal run of a new `RunKind.TASK` does the
work, and that run's state. The worker is an ordinary engine run, so it gets the queue, cancel, steer, resume,
budgets, `_wrap_up` and the confirmation pause for free. A new `features/tasks.py` (`TaskManager` plus `TasksTools`)
listens on the bus the way `CallMe` does (`features/callme.py:61-117`). It keeps the task row up to date, publishes a
global `task.changed` event, writes the report as an assistant message into the parent chat and sends one push. The
web keeps `tasks` in the store and renders a `TaskCard` in the transcript after the run that delegated it. On that
card it reuses `ConfirmCard`, which carries the **worker's** run id. `tool.confirm` is not limited to the
conversation it came from (`api/ws.py:127-128`), so a confirmation can be answered from the parent chat with no new
core code. Call noting adds a `call_id` to voice runs and a core-side `calls` table with an exactly-once claim at
hang-up or drop.

Why a separate work conversation and not a child run in the asking chat: there is no per-conversation
serialization (as-is constraint 3). A child run would interleave with Arsen's next turn, and both histories would
fill with each other's tool output. The card is the only thing that appears in the asking chat.

## 1. Reuse map (as-is → what we do with it)

| As-is | Reuse |
|---|---|
| `RunEngine.create_run` (`run_engine.py:142-215`) | Starts the worker in a work conversation that already exists (`conversation_id=` given, `kind=RunKind.TASK`) |
| cancel/steer/confirm (`run_engine.py:217-290`) | Task cancel / steer / approve call these with `task.run_id` |
| `_wait_for_confirmation` (`loop.py:960-987`) | Pauses the task with no change, **once TASK is not in `_UNATTENDED`** (`loop.py:94`) |
| `_finish`/`_wrap_up` (`loop.py:1097-1138`) | The "stopped here" report at budget end (EC-13) |
| `_resume_on_boot` (`run_engine.py:128-138`) + resumed-mutation question (`loop.py:734-750`) | EC-4: tasks resume, and a half-done mutation becomes a confirm on the card |
| `CallMe` subscriber pattern + `notify.reach` (`callme.py:61-157`, `notify.py:105-114`) | `TaskManager` subscriber; the push uses `type:'notice'` + `path` (sw.js already handles it → **no sw.js change, no SHELL bump**) |
| `schedule_fires` UNIQUE claim (`schedules.py:215-224`) | `calls.ended_at IS NULL` claim, `tasks.status='noted'` → `queued` CAS |
| `provider_health()` / `cannot_route()` (`registry.py:184-241`) | Health block + the `needs` check in `tasks.delegate` |
| `files.attach` → `take_outgoing` at the final answer (`loop.py:1187-1193`) | The worker attaches to its final answer. The report writer **re-binds** those attachments onto the report message (`AttachmentStore.bind`, `attachments.py:308`; it moves the row, and incognito is honoured) |
| `Planner`/`PlanChecklist` | The DoD is shown with the same checklist look (`PlanChecklist.tsx`); a new `DodList` with the same CSS classes |
| `ConfirmCard` + `store.confirmTool` | Embedded in `TaskCard` unchanged |
| `RunInspector` | "Open work log" on the card = navigate to the work conversation (no new inspector) |
| journal incognito skip (`journal.py:258`) | Work conversation inherits `incognito` → EC-9 is handled by data, not by new code |

## 2. Files to change / create

### proto (2.0.0a32 → a33 in slice 1, a34 in slice 2)
- `runs.py:16-23` add `RunKind.TASK="task"`; `:150-157` add `ConversationKind.TASK="task"`; `Run` (`:119-147`) add
  `call_id: str | None = None` (slice 2).
- **new** `tasks.py`: `TaskStatus` (noted, queued, running, waiting_user, needs_input, done, partial, failed,
  cancelled, interrupted), `DodItem{text, done: bool|None, evidence: str|None}`,
  `TaskBrief{goal, context, constraints, dod: list[DodItem] (1-8), needs_approval: list[str], needs: list[str]}`,
  `Task{id, title, status, brief, parent_conversation_id, work_conversation_id, origin_run_id, run_id, call_id,
  pending_confirm: {run_id, call_id, name, arguments, reason} | None, question: str|None, report_message_id,
  error, incognito, created_at, started_at, finished_at}`. Export from `__init__`.
- `events.py`: `TaskChanged(_Base){type:'task.changed', task: Task}`. This is not a `RunEvent`, so `bus.publish`
  sends it to every subscriber (`engine/bus.py:59-77`). Add it to the `ServerEvent` union (`:294-332`). Client
  messages (`:431-442`): `TaskCancelRequest{task_id}`, `TaskSteerRequest{task_id, text}` (slice 1),
  `CallStarted{call_id, conversation_id}`, `CallEnded{call_id, reason:'hangup'}`, `CallAlive{call_id}` (slice 2).
  `RunCreateRequest` gets `call_id: str|None` (slice 2).
- `settings.py`:
  - `_default_routing` (`:136-147`): `TASK → BACKGROUND` (EC-6: voice and typed keep the chat lane).
  - `_default_budgets` (`:150-159`): `TASK: RunBudget(max_steps=60, max_tokens=800_000, max_seconds=2400)`.
  - **new** `TasksSettings{enabled=True, max_running=2 (open Q5), max_depth=1, call_idle_end_s=180,
    call_alive_s=45}` added to `Settings`.
  - **Stored-settings fix (blocking):** settings are stored per top-level key (`db/store.py:605-618`). A stored
    `budgets`/`run_routing` dict therefore has no TASK entry. Routing falls back to CHAT through `.get`
    (`settings.py:783`), and the budget falls back to the generic 25-step/600 s default through `.get`
    (`run_engine.py:196`). Add a `model_validator(mode="before")` "kinds added later get their default". It is the
    same pattern as `_roles_added_later_inherit_chat` (`settings.py:764-773`). With it, MAIL gets a budget for free.
  - `VOICE_STYLE_DEFAULT` (`:494-507`): replace `do what he asks now, never "after the call"` with the
    noting rule (slice 2). The stored copy overrides the default, so `VoiceSettings` gets a before-validator. It
    swaps the stored `style` for the new default **only when the stored text equals a previous built-in default**
    (`_PREVIOUS_VOICE_STYLES = (old text,)`). This is data versioning, not a keyword filter. A style Arsen edited
    himself is left alone, and Settings UI shows a "a newer default exists" hint (optional).

### core (2.0.0a92 → a93 / a94 / a95 / a96 over the slices)
- `db/migrations/0017_tasks.sql` (slice 1): table `tasks` (columns as `Task`; `brief` / `pending_confirm` as JSON
  text; `parent_conversation_id … REFERENCES conversations(id) ON DELETE CASCADE`; indexes on
  (parent_conversation_id, created_at), (work_conversation_id), (status)).
  `0018_calls.sql` (slice 2): `calls(id PK, conversation_id, started_at, last_seen_at, ended_at, end_reason)`,
  `ALTER TABLE runs ADD COLUMN call_id TEXT`, `tasks.call_id` index. Keep the two files apart so slice 1 ships on
  its own. Store: `create_run` insert list and `_run()` mapping (`db/store.py` ~L515-540) gain `call_id`;
  `save_run` (`:541-559`) does not change (call_id is set once).
- `engine/run_engine.py`
  - `_PRIORITY` (`:56-63`): add `TASK: 2` (Arsen asked for it, so it goes ahead of scheduled and behind chat/collab)
    **and `MAIL`** (the gap blocks: the guard test below iterates over `RunKind` and fails on MAIL).
  - `create_run` (`:164`): the conversation-kind derivation already works for TASK. Add the `call_id` param (slice 2).
  - `_dispatch` (`:334-341`): a **per-kind cap**. Count the TASK runs in `_active` that are not waiting. When the
    count reaches `settings.tasks.max_running`, hold the run the same way the call gate does (`held.append`).
    TASK stays **out of** `_BESIDE_A_CALL` (`:54`), so a task never starts mid-call and voice stays first.
  - `call_urgency` (`:356`) needs nothing: a TASK run is BACKGROUND already.
- `engine/loop.py`
  - `:94` `_UNATTENDED` stays as is: TASK is **not** in it, so the confirmation pause applies (US-7). Add a
    comment saying why.
  - `:95` `_CONTEXT_KINDS` add TASK (skills, planner and the KG learner work on the brief; incognito is already
    handled at `:572`).
  - `_exposed_tools` (`:195-208`): when `run.kind is TASK`, leave out `tasks.delegate` / `tasks.note` (EC-17:
    depth 1). The tool also refuses in code (see below), because a model can name a tool it was not offered
    (the same reasoning as `:760-765`).
- `engine/context.py`
  - System parts: next to the SCHEDULED block (`:505-515`), add a `## This task` block for TASK runs. It says
    nobody is watching live; Arsen reads the final reply as the report; write it in English; call
    `tasks.report` with every DoD item; actions that need approval will pause for Arsen, so call them normally;
    information only Arsen has goes to `tasks.ask`, never a guess. The block is constant per task, so it caches.
  - **new** `TasksBlock` (per-turn, the same rule as `VoiceBlock` `:815-830`). It returns None unless this
    conversation has open tasks, or (on a voice run) this call has noted tasks. It lists them with id, title and
    state, so the model sees duplicates (EC-2), can amend or drop (US-4), and can answer "what are you working
    on?" (US-6). Tasks that finished since this conversation's last run are marked "new", so a call can mention
    them at a pause (EC-8).
  - **new** `HealthBlock` (slice 3, per-turn, CHAT and voice runs only). One line per provider that is
    `ok=False` in `provider_health()`, with its error. It is empty (zero tokens) when everything is up, so the
    prefix cache stays intact. Wire both in `app.py:150-158`.
- **new** `features/tasks.py`
  - `TaskStore` (raw SQL, the `ScheduleStore`/`DayJournal` pattern): create, get, list_for(parent),
    list_open(), list_for_call(call_id), `claim(task_id, from_status, to_status) -> bool` (CAS `UPDATE … WHERE
    status=?`, using the rowcount).
  - `TaskManager`: `Subscriber("tasks", everything=True)`, the same skeleton as `CallMe.start/stop/_consume`.
    It maps a worker `run_id` → task through `tasks.run_id`, and a work conversation → task. It handles:
    `RunStarted/Resumed` → running; `RunWaitingUser` + the matching `ToolConfirmRequested` → waiting_user +
    `pending_confirm` + push; `ToolConfirmResolved` → clear; `RunDone` → `_report()`;
    `RunFailed` → failed + report; `RunCancelled` → cancelled + a "stopped here" report built from the plan and
    the last assistant text (no model call, EC-12); `RunInterrupted` → interrupted (the card shows it; the engine
    resumes it). Each change saves the row and then publishes `TaskChanged`.
  - `start(task)` creates the work conversation (`store.create_conversation(kind=TASK, title, folder_key=task.id,
    incognito=parent.incognito)`), renders the brief to text, and calls `engine.create_run(kind=TASK,
    call_me=False)`. TaskManager sends the push itself.
  - `_report(task, run)`: reads the structured result that `tasks.report` stored on the task (DoD per item). The
    outcome is **done only if every DoD item is reported done**. Otherwise it is partial, or failed when nothing
    was reported. If `tasks.report` never ran (a budget wrap-up), the status is `partial` with "the DoD was not
    confirmed". This is the US-3 "never done for something not done" rule, written in code. The report message is
    `Message.assistant(final_text)` written into the **parent** conversation with `run_id=None`. The worker's
    final-message attachments are re-bound onto it (EC-10: short text + files; the full text stays in the work
    conversation). Then: `bus.publish(MessageCreated)`, mark the parent unread when nobody is watching (the
    `_after_run` logic in `run_engine.py:401-408`), push through `notify.reach`, and `journal.note`-equivalent
    for the parent (a public `DayJournal.touch(conversation_id)`, because the report is not a run).
  - `on_boot()`: tasks in `queued` whose run is gone (crash between the row and the run) are started again.
    `noted` tasks of calls that ended get the sweeper (slice 2).
  - `TasksTools(BuiltinProvider)` (registered like `SessionsTools`):
    - `tasks.delegate(title, goal, context, constraints, dod[], needs_approval[], needs[])`: refused unless
      `current run.kind is CHAT` (and depth: never from TASK). Refused with `cannot_route`-style text when a
      `needs` namespace is unhealthy (slice 3). When called from a **voice** run it creates a *noted* task
      instead (one tool for the model, the channel decides; the model chooses between `tasks.note` and delegate
      only by meaning). It returns the task id and "a card is shown in the chat; tell Arsen in one line what you
      will deliver".
    - `tasks.note(...)` (slice 2): the same args, status `noted`, `call_id` from the current run.
    - `tasks.list()` read-only, `tasks.cancel(id)`, `tasks.update(id, text)`: when the task is noted, edit the
      brief; when it is running, `engine.steer(run_id)`, or else a new TASK run in the work conversation (EC-14).
      `tasks.drop(id)` for noted tasks.
    - `tasks.report(outcome_text, dod:[{index, done, evidence}])`: only for TASK runs; stores the result on the row.
    - `tasks.ask(question)`: only for TASK runs. It sets `needs_input` + `question`, and the run then finishes
      with the question as its reply. Arsen's answer in the parent chat → the model calls `tasks.update(id, answer)`
      → a new run in the work conversation.
  - `CallLedger` (slice 2): `started(call_id, conv)` (INSERT OR IGNORE, sets last_seen), `alive(call_id)`,
    `ended(call_id, reason)`: `UPDATE calls SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL`. If the
    rowcount is 1, then for each `noted` task of the call: `claim(noted→queued)` → `start()`. A sweeper every 30 s
    ends calls whose `last_seen_at` is older than `call_idle_end_s` with reason `dropped` (EC-3). The ws
    disconnect itself never ends a call (a reconnect is not a hang-up). The after-call report: when every task of
    a call is terminal or waiting, one summary message goes into the call's conversation with a line per task
    (done / waiting / not done + why), plus one push.
- `api/ws.py:86-128`: dispatch `TaskCancelRequest`, `TaskSteerRequest` (slice 1); `CallStarted/Ended/Alive`;
  pass `call_id` into `create_run` and into the late-steer path `:111-119` (slice 2). Each voice
  `run.create`/`run.steer` with a call_id also counts as `alive`.
- `api/rest.py`: `GET /api/tasks?conversation_id=&open=`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/cancel`,
  `POST /api/tasks/{id}/steer` (slice 1).
- `app.py`: build `TaskStore/TaskManager/TasksTools`, add them to the registry, `start()` after `callme.start()`
  (`:281`), `stop()` before `engine.stop()`. Add the context blocks (`:150-158`).
- `features/journal.py:47-53` `_KIND_LABEL[TASK]="task"`. Task conversations stay in the overview (it is how "what
  is waiting on me" sees a parked task); incognito work conversations are skipped by the existing filter (`:258`).
- `features/sessions.py`: `_say` refuses a TASK conversation ("steer it with tasks.update"). `ADDRESSABLE_KINDS`
  stays as it is, and the 2026-09-19 docstring stays as it is. A sub-agent is started only by `tasks.delegate`
  / `tasks.note`, whose card is the announcement (Gate A "only announced tasks", enforced in code because the
  card is created inside the same tool call that starts the work).
- `features/expiry.py:82-90` `delete_conversation`: also delete the work conversations of the chat's tasks
  (cancel their runs first). Otherwise deleting the parent leaves orphan work chats behind.
- `features/office.py:351` `subagentToolNames: ["tasks.delegate"]` (cosmetic, optional).

### web (2.0.0-alpha.73 → .74 / .75 / .76 / .77)
- `protocol/types.ts`: `RunKind` (`:58`, add `'task'` **and the missing `'mail'`**), `ConversationKind` (`:144`),
  `Run.call_id`, `Task`/`TaskBrief`/`DodItem`/`TaskStatus`, `TaskChanged` added to `ServerEvent` (`:1046`), the new
  client messages added to `ClientMessage` (`:1126`). Hand-mirrored, so the contract test below checks it.
- `store/state.ts:25-43`: `tasks: Record<string, Task>`. `store/reducer.ts:19-44`: `case 'task.changed'`
  upserts (a pure function, tested). Keep the event out of the `default: runScoped` path.
- `store/store.ts`: fetch `/api/tasks?conversation_id=` in `openConversation`; actions `cancelTask`, `steerTask`.
  Slice 2: `startCall` (`:430`) makes a `callId`, sends `call.started` after the subscribe (`:535`), passes
  `call_id` in `transport.create`, runs a `call.alive` interval (`call_alive_s`), and re-sends `call.started` on
  ws reopen. `endCall` (`:552`) sends `call.ended` **before** `session=null`.
- `store/selectors.ts`: `selectOpenTasks(conversationId)`. Leave `'task'` out of the sidebar folders (`:8`)
  (the work chats are reached from the card, not from the sidebar).
- `store/transcript.ts`: `TranscriptSource.tasks`, a new item `{kind:'task', key, taskId}` placed right after the
  origin run's block (`origin_run_id`), or by `created_at` when that run is not loaded. The report message stays
  a normal `message` item.
- **new** `chat/TaskCard.tsx`: title, status pill, elapsed (`useTicker`), DoD list, collapsible brief, Cancel,
  "Open work log", and an embedded `<ConfirmCard runId={task.pending_confirm.run_id} …/>` while it is
  waiting_user. `chat/Transcript.tsx:160-179` gets a `case 'task'`.
- **new** `chat/TasksChip.tsx` in `shell/TopBar.tsx:59-61` and `:71` (next to the active-run dot). It is hidden
  at 0 and opens a small popover list.
- Slice 2: `voice/session.ts:47-65` `CallState.callId`; **new** `voice/NotedList.tsx` mounted in
  `voice/CallScreen.tsx` after `<Captions/>` (`:141`).
- `styles`: card and pill classes, checked at 360 px and in light/dark (EC-15).
- `mock/server.mjs`: emit `task.changed` so `npm run mock` shows a card.

## 3. Slices (each one deployable and useful alone)

### Slice 1 — Typed delegation, task card, honest report (US-1, 2, 3, 6, EC-1, 4, 9, 10, 12, 13, 16, 17)
Scope: `RunKind/ConversationKind.TASK`, settings backfill validator, `_PRIORITY` (+MAIL), per-kind cap, 0017,
`features/tasks.py` (store, manager, `delegate/list/cancel/update/report/ask`), the `## This task` system block,
`TasksBlock` (open tasks only), REST + ws task cancel/steer, `task.changed`, web card + chip + reducer, push on
finish. **Confirmations already pause** (US-7 comes almost for free: TASK is not unattended), and the card shows
the ConfirmCard. Without it a background task could not even send a draft, so it belongs here.
Effort: core 2.5 d, web 1.5 d, tests 1 d, alpha + live check 0.5 d → **~5.5 d**.

### Slice 2 — Call noting, call lifecycle, after-call report (US-4, EC-3, EC-8, EC-11)
Scope: `Run.call_id` + 0018, `CallStarted/Ended/Alive`, `CallLedger` + sweeper, `tasks.note/drop`, the voice
path of `delegate`, `TasksBlock` noted list on voice turns + the "read the list back when he wraps up"
instruction, new voice style default + the stored-default upgrade validator, the after-call summary message and
one push, web `callId` / heartbeat / `call.ended`, `NotedList` on the call screen.
Effort: core 2 d, web 1.5 d, tests 1 d, headless call alpha 1 d → **~5.5 d**.

### Slice 3 — Honesty / health (US-5, EC-5, EC-7)
Scope: `HealthBlock`; the `needs` check in `delegate`/`note` against `provider_health()` and `_last_known` (an
offline host → refusal text carrying the provider's own error); the take / ask / decline guidance in the
`tasks.delegate` description and the system block (can / knows / understands); `tasks.ask` → `needs_input` card
state with the question and an "Answer" box that steers. No word lists: the model judges, and only the real
status is checked in code.
Effort: **~2 d**.

### Slice 4 — Confirm proxy polish and steering (US-7 end to end, EC-14)
Scope: "waiting on you" push with the action in one line (`name` + recipient from `arguments`); Approve / Deny
from the card when the parent chat is open on another device (it already works through ws, so this slice adds
tests and UX); the restart case (parked worker + `engine.confirm` re-queue, `run_engine.py:280-290`); the
resumed "did it complete?" confirm explained on the card (constraint 8); a steer box on a running card.
Effort: **~1.5 d**.

Total ≈ 14–15 dev-days including tests and alpha checks. Slices 3 and 4 can swap places.

## 4. Test strategy

### Per slice (pytest, `tests/conftest.py` Harness + `FakeTurn`)
New `tests/test_tasks.py`. The chat fake serves both the parent and the worker. Turns are scripted in order, and
`max_running` in the harness is set so the order is deterministic.
- S1: delegate creates a task + a work conversation (kind TASK, folder_key = task id, incognito inherited) + a
  queued TASK run; the parent run finishes at once (the chat is free). The worker scripted with `tasks.report`
  (all done) → status done, a report message in the **parent** with `run_id=None`, `task.changed` seen by a
  subscriber of a *different* conversation (global), the push recorded through the `_transport` stub from
  `test_callme.py:49-62`. One DoD item reported not done → `partial`. No `tasks.report` + budget end → `partial`,
  never done. Worker failure → failed + reason. Cancel while running → cancelled + "stopped here" text. Two
  delegates in one turn → two tasks (EC-1). Delegate from a TASK run → refused, and not in the exposed tools
  (EC-17). An incognito parent → incognito work conversation, no digest, not in `sessions.list`, attachments
  kept off disk (EC-9). `files.attach` in the worker → the attachment ends up on the parent report message (EC-10).
  `harness.restart()` mid-task → resumed and finished, the card goes running → interrupted → running → done (EC-4).
  A destructive tool in the worker → `waiting_user` with `pending_confirm`; `engine.confirm(task.run_id…)`
  approves → done. Deny → the report says it was rejected. The per-kind cap: with 3 delegates and `max_running=2`,
  the third stays queued until one ends.
- S2: a voice run with call_id → `tasks.note` → `noted`, nothing runs; `call.ended` twice → the tasks start
  **once**; a lost `call.ended` → the sweeper ends the call after `call_idle_end_s` (set to 0.2 s in the test)
  with reason dropped; `alive` keeps it open; restart between noting and ending → the sweeper still starts them
  once. Drop / update of a noted task. The after-call summary is written only when every task is
  terminal/waiting. Voice style validator: stored old default → new default; a customised style is kept.
- S3: a provider with `error` → `HealthBlock` shows it and `delegate(needs=[that])` is refused with its error; all
  healthy → the block is None (assert byte-identical context, so the cache is not broken).
- S4: the restart-while-parked confirm path; the resumed mutating call on a task shows `pending_confirm` with
  the "did it complete" reason.

### Guard and regression tests (behaviour we touch)
- **Every `RunKind` has a priority, a lane and a budget**, and `create_run` works for each (parametrized over
  `RunKind`). This test fails today on MAIL, which is why MAIL gets fixed in slice 1.
- Settings backfill: stored `budgets`/`run_routing` without TASK → TASK defaults; stored values kept.
- Engine: the call gate still holds a SCHEDULED run during a voice run and now also holds TASK (`test_engine.py`
  pattern); waiting_user still holds no slot.
- Confirmations: SCHEDULED/TRIAGE stay unattended (no pause); CHAT unchanged; TASK pauses
  (`test_loop.py` confirmation tests).
- Sessions: the existing `test_sessions.py` passes unchanged; `sessions.say` to a TASK conversation is refused.
- Schedules: `test_features.py` schedule fires are unaffected (a new kind in the heap; priorities re-ordered:
  assert SCHEDULED still runs after CHAT).
- Call-me: worker runs have `call_me=False` and never ring through `CallMe`; a parent chat with the bell on still
  rings on its own run (`test_callme.py`).
- Journal: a TASK conversation gets a digest labelled "task"; an incognito one does not.
- Expiry: deleting the parent removes the work conversations and cancels their runs.

### API contract
Extend `tests/test_api_contract.py` with the tasks routes (list by conversation, get, cancel, steer, 404 on an
unknown id). Add a **proto ↔ web mirror check**: a small pytest that reads `web/src/protocol/types.ts` and asserts
that every `ServerEvent`/`ClientMessage` `type` literal and every `RunKind`/`ConversationKind` value in proto
appears there. It is cheap, and it would have caught the missing `'mail'` in web `RunKind`.

### Web (vitest)
- `reducer.test.ts`: `task.changed` upsert, ordering, terminal → no stream left behind, and that it does not
  touch `runs`.
- `transcript.test.ts` (new): the task item lands after the origin run block; the origin is not loaded →
  ordered by created_at; the report message renders as a normal message.
- `session.test.ts` (or a store test): `call.ended` is sent once on `endCall`, `call.started` again after a
  reconnect, and `run.create` carries the call_id.

### Alpha / end to end (per memory `jarvis2-alpha-test`)
Build `jarvis2-core:alpha` from the working tree on ardi and run it on `127.0.0.1:9121` with its own volume.
PATCH only the live `roles`/`voice`/`stt_*` settings (no MCP, no mail, no Discord, so nothing can leave the
house). Use the real model, because delegation is a model-judgement feature and FakeTurn proves only the
plumbing.
- Typed: in Playwright (mobile and desktop viewport, both themes) ask for two long things in one message → two
  cards, the chat answers within one reply, cards go to done, the report appears, the chip count drops. Cancel
  one. A destructive step (`schedule.create` is destructive and harmless) → the card waits → Approve.
- Voice headless: Chromium with `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream
  --use-file-for-fake-audio-capture=/w/call.wav`. The WAV is OmniVoice-made: silence, then "Find me three hotels
  near the Rathaus in Vienna for 14 to 16 November and check trains from Budapest", silence, then "what have you
  got so far?". Check that NotedList shows 2 items, click hang up, and assert through `/api/tasks` that both
  started exactly once and that the after-call summary appears. Repeat with the page closed **without** hang-up
  (a drop) → started by the sweeper after `call_idle_end_s` (set to 30 s on alpha).
- Restart during a task: `docker restart jarvis2-alpha` → the card shows interrupted → resumed.
- Push cannot be proven headless. Register a fake device with endpoint `https://127.0.0.1:1/…` and read the
  "push to … failed" log lines to see the attempts. The real ring is checked on Arsen's phone after deploy.
- Then the live check in Brave on production with a real conversation, mid-run (memory `jarvis2-verify-live`),
  and a screenshot asked for early.
- Remove the container and the volume afterwards.

## 5. Process per slice
Full `uv run pytest -q` + ruff check/format + pyright + `npm run lint && npm test && npm run build` green →
bump core/proto/web by hand → commit with only own files staged (the tree is shared), the message listing the
versions → push main → `bash scripts/deploy_ardi.sh` → live check. Migrations 0017 (slice 1) and 0018 (slice 2)
are additive only (new tables, one nullable column), so a rollback image still boots: older code ignores the
extra column and tables.

## 6. Tech debt — fix only what blocks
- **Blocks:** `_PRIORITY` without MAIL (KeyError, in the same dict we edit); stored `budgets`/`run_routing` hiding
  new kinds (TASK would silently run on 25 steps on the CHAT lane); web `RunKind` missing `'mail'` (we edit the
  same union).
- **Blocks slice 2:** `VOICE_STYLE_DEFAULT` contradicting US-4, and the stored copy masking it.
- Not blocking, leave it: no per-conversation serialization (we avoid it with work conversations);
  `proto/schema.py` unused (the mirror test is the cheap substitute); in-memory session chains.
- Small: `run_engine.py:197` constructs a throwaway `Run` to get the default budget; replace it with `RunBudget()`
  when touching the line.

## 7. Risks from the implementation side
1. **Model judgement quality.** Whether qwen on the 27B delegates sensibly (not for "what time is it", not
   inline for a 20-step job) and writes a usable brief can only be proven on alpha with the real model. A bad
   brief gives a confident wrong result. Mitigation: a strict `tasks.delegate` schema (DoD 1-8 items, required
   context); the parent run's turns are the only source, so the brief must include links and names; measure on
   alpha with 10 realistic asks before slice 1 ships.
2. **GPU contention.** TASK on the BACKGROUND lane, where scheduled runs also go and housekeeping runs. With 2
   concurrent tasks + a newsletter, KV prefix eviction returns (settings comment at `:681-687`). Start with
   `max_running=1` and raise it after measuring TTFT on the chat lane.
3. **Call end detection.** Hang-up vs drop is now explicit, but Android Chrome can kill the page without sending
   `call.ended`. Then the start depends on the sweeper, which delays it by `call_idle_end_s`. If the sweeper ends
   a call that is only silent, tasks start mid-call (heartbeat bugs). Exactly-once is guaranteed by the CAS. Early
   or late start is a tuning issue; log `end_reason` and watch it.
4. **Report placement in history.** A `run_id=None` assistant message in the parent changes that chat's history
   tail; the next turn re-prefills only the tail (fine). If a parent run is mid-flight, the report is not in its
   in-memory messages; the run learns about it on the next turn, which is acceptable. Check that the compactor
   and `last_turn_skills` tolerate assistant messages without a run_id (verify in slice 1).
5. **Confirm across devices and restarts.** A worker parked on `waiting_user` across a redeploy relies on
   `engine.confirm`'s DB path (`run_engine.py:280-290`), which re-enqueues as resumed. The card must therefore
   rebuild `pending_confirm` from the task row, not from in-memory state. This is covered by a test, but it is
   the path most likely to break silently.
6. **Push volume.** Done + waiting + after-call summary pushes could be 3-4 per call. Group them like CallMe
   (gather window) and send one push per task terminal state, plus the after-call summary instead of per-task
   pushes when a call_id is set.
7. **Web transcript complexity.** `transcript.ts` is already the most fragile web file (the 2026-10-06 repaint
   incident). TaskCard must not re-render on every stream tick; select tasks shallowly by conversation and
   avoid any DOM or highlight work per event.
8. **Hand-mirrored types.** Three packages bump together in every slice. A missed mirror shows up only at
   runtime on the phone, which is why the mirror test is in slice 1.
