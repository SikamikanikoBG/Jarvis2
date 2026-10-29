# 03 — As-is map (2026-10-29, main @ aa7128a)

Paths: core = `packages/core/jarvis_core/`, proto = `packages/proto/jarvis_proto/`, web = `web/src/`.
Baseline: `uv run pytest -q` green (core+host+proto). Versions: core 2.0.0a92, proto 2.0.0a32,
host 2.0.0a27, web 2.0.0-alpha.73 (bumped by hand in each pyproject / package.json; commit
message lists changed packages). No CHANGELOG.md (history = commits, docs/journal*.md, docs/WAVE2.md).
Stories: `docs/stories/NN_name.md`, next free = **13**.
GitHub: issues #1 (vLLM lessons), #9 (SIM calls, parked). Nothing on tasks/delegation. Commits go to main.

## Nothing like delegation exists
No subagent/delegate/todo/task concept in core (`features/office.py:351` advertises `subagentToolNames: []`).
No outward path to Claude Code / cloud agents (Claude reaches Jarvis only inward via `/mcp` collab key).

## 1. Runs — the executor we'd reuse
- Model `proto/runs.py`: `RunKind` L16-23 (chat, scheduled, collab, triage, meeting, mail, system),
  `RunStatus` L26-38 (incl. waiting_user, interrupted), `Run` L119-147 (budget, priority, plan,
  waiting_reason, channel, call_me). **No parent_run_id / task id / metadata.**
- Create `engine/run_engine.py:142-215` (`create_run`): conversation kind defaults to
  `ConversationKind(kind.value)` (L164) → a new RunKind needs a ConversationKind or explicit kind.
  Budget from `settings.budgets` (L196), priority `_PRIORITY` (L56-63). Callers: ws `run.create`
  (`api/ws.py:86-110`), `sessions._deliver` (`features/sessions.py:341`), `collab.run_and_wait`
  (`features/collab.py:71-117`), schedules (`app.py:246-270`).
- Queue: heap (priority, created_at) L308-314; `_dispatch` L323-349; `JARVIS_MAX_RUNS` default 3;
  waiting_user runs don't hold a slot (L384-386). **Call gating** L52-54/L334-341: while a voice run
  *executes*, only CHAT/SYSTEM runs start. **No per-conversation serialization.**
- Cancel L217-242, steer L244-256 (`engine/control.py:25`, read in loop L326-330, L545-564),
  confirm L276-290, end `_execute` L351-382 / `_after_run` L395-419 → `app._run_finished` (`app.py:198-207`).
- Lanes: `proto/settings.py:136-147` run_routing, `lane_for` L783; `models/factory.py`; urgency
  `models/base.py:85-136` (VOICE > TYPED > BACKGROUND; voice may take +1 slot; 2 per endpoint, settings L674).
  Budgets per kind `_default_budgets` (settings L150-159).
- Restart: `stop()` L112-126 → interrupted; `_resume_on_boot` L128-138 re-enqueues; resumed run with
  an unanswered mutating call asks "did it complete?" (`engine/loop.py:734-750`).
- Debt: `MAIL` RunKind missing from `_PRIORITY`, routing and budgets (create_run(kind=MAIL) would KeyError).

## 2. AgentLoop
- `engine/loop.py:216` entry; tools `_exposed_tools` L195-208 (internal filter, incognito, voice
  namespaces, ExposurePolicy, PLAN_TOOLS). Planner = per-run checklist (`features/planner.py`,
  `PlanStep`, `jarvis.plan_step_done`) — nearest thing to a DoD.
- Budgets `engine/supervision.py:120-135`; budget end → `_finish` L1097-1117 → `_wrap_up` L1119-1138
  ("what you found, what you did, what is left undone ... Do not promise to continue").
- Judge/guards: supervision L139-207, loop L433-608, L1256-1266.
- **Confirmations**: `proto/settings.py:435-470` `needs_confirmation`; `_UNATTENDED = {SCHEDULED,
  TRIAGE, MEETING, SYSTEM}` (`loop.py:94`) → **unattended runs never ask and never pause** (the
  2026-10-15 reboot gap). Text path `_wait_for_confirmation` L960-987 (ToolConfirmRequested +
  RunWaitingUser), answer via ws `tool.confirm` (`proto/events.py:401-406`). Voice path L989-1016.
  Mail policy rewrites unapproved sends into drafts (L796-807).

## 3. Conversations, sessions, schedules, journal, home
- `Conversation` (`proto/runs.py:187-220`): kind, folder_key/label, incognito, ttl, activity. No parent/child.
- Schedules: each fire = own SCHEDULED conversation, folder_key = schedule id
  (`features/schedules.py:324-337`); `schedule_fires` UNIQUE slot → exactly-once (L215-224).
  Context tells scheduled runs "nobody is present" (`engine/context.py:505-515`).
- Sessions (`features/sessions.py`): list/read/today/say L194-326; chain + `max_hops` (L173-177,
  L316-323), chains in memory only. 2026-09-19 rule in docstring L16-19 and tool description L293-296.
- Collab `run_and_wait` (`features/collab.py:71-117`) — create run + await on bus.
- Journal (`features/journal.py`): `note(run)` L110-121, digests table (0015), `overview()` L249-320
  skips incognito; `TodayBlock` L323-338 on calls/home.
- Home conversation: `app.py:213-231`, `settings.assistant.home_conversation_id` (settings L582),
  `POST /api/assistant/home`, home block `engine/context.py:479-488`.

## 4. Voice calls
- **Core has no call session**: a call is client-side (`web/store/store.ts:430-560`,
  `web/voice/session.ts`); each utterance is ws `run.create` channel=voice or `run.steer`.
- **Hang-up is not signalled to core** (`store.ts:552-559 endCall`, `session.ts:276-287`). Drop and
  hang-up look the same. `on_a_call()` (`run_engine.py:316`) = a voice run executing, false between turns.
- Voice run: think off, no plan, namespace filter, spoken confirmation, `VoiceBlock` (`context.py:815-830`).
- **`VOICE_STYLE_DEFAULT` (`proto/settings.py:494`) says "do what he asks now, never 'after the
  call'"** — contradicts US-4. Settings stored per top-level key (`db/store.py:605-618`), so the
  deployed `voice.style` copy will not pick up a changed default (needs a migration of the stored value
  or a separate block).
- Call-me (`features/callme.py`): bus subscriber; rings on RunDone/RunFailed/RunWaitingUser for
  `call_me` runs (L85-117), skips while the chat has viewers (L101), batches 4 s (L119-157);
  payload `{type:"call", ...}` → `notify.reach`. `jarvis.call_me` tool `tools/builtin.py:200-223`.

## 5. Notifications / files
- `notify.reach` (`features/notify.py:105-114`) push → Discord fallback; `webpush.py`; `api/push.py`.
- `files.attach` (`features/files.py:50-87`) binds to the current run's final answer (`loop.py:1187-1193`)
  → lands in the **run's own** conversation.
- Web SW `web/public/sw.js:49-67` `type:'notice'` with `path`; cache `SHELL='jarvis-shell-v8'` bump on change.

## 6. Tool / host availability (for "can I do this")
- `tools/registry.py`: last-known tool sets persisted (0007), `_errors` L126-182,
  `provider_health()` L184-192, `cannot_route()` L209-241 ("X is not reachable right now").
- MCP re-list every 30 s (`app.py:308-322`); `/api/status` (`api/rest.py:38-64`); host `host_status` tool.
- **Today the model learns a host is down only by failing a call.** No context block carries health.

## 7. Events / protocol / web
- Run-scoped events (`proto/events.py`) go to subscribers of that conversation; conversation-level
  events (conversation.updated, message.created, run.updated, *.changed) go to all (`engine/bus.py:59-77`).
  `ServerEvent` L294-332, client messages L431-442.
- `Message` (`proto/messages.py:70-85`) has name/attachments/channel, **no metadata**.
- Web types: **hand-mirrored** in `web/protocol/types.ts` (1228 lines; `proto/schema.py` exists but unused).
- Web chat: `chat/ChatScreen.tsx:258-327`; `chat/Transcript.tsx:160-179` switch on item kind;
  `store/transcript.ts:42-58` TranscriptItem union, `buildTranscriptFrom` L97, confirm items L182-196.
  Reusable: `ConfirmCard.tsx` (+ `store.confirmTool` store.ts:749), `PlanChecklist.tsx` (DoD list),
  `RunChip.tsx` (status/ticker), `RunInspector.tsx` (brief view), `MessageItem` + `Attachments`.
  Header `shell/TopBar.tsx:57-74` (slot next to the active-run dot). Store `store/state.ts:25-43`,
  reducer `store/reducer.ts` (+ 574-line reducer.test.ts). Call screen `voice/CallScreen.tsx:54-210`
  (noted list slot ~L140, between Captions and controls); `CallState` `voice/session.ts:47-65`.

## 8. DB
- Migrations `db/migrations/0001..0016.sql`, run once (`db/connection.py:47-65`); next = **0017**.
  Feature stores own raw SQL (pattern: `ScheduleStore`, `DayJournal`). `save_run` updates a fixed column
  list (`db/store.py:541-559`).

## 9. Tests / CI / deploy
- Python: `packages/*/tests`, core `conftest.py` Harness + `FakeTurn` fake model, `test_api_contract.py`.
- Web: vitest `src/**/*.test.ts` only (no component tests); Playwright configured, no e2e dir; `npm run mock`.
- CI `.github/workflows/ci.yml`: ruff check + format --check, pyright, pytest --timeout 60; web lint/test/build; docker build.
- Deploy: `bash scripts/deploy_ardi.sh` (packs the working tree → docker build on ardi → restart, waits /api/health).
  Alpha instance pattern: memory `jarvis2-alpha-test` (`jarvis2-core:alpha` on :9121).

## Reuse vs new
**Reuse:** RunEngine (queue, cancel, steer, resume, confirm, call_me), budgets + supervisor + `_wrap_up`,
`notify.reach`/CallMe, `files.attach`, `run_and_wait`/bus waiting, sessions hop guard (depth),
`schedule_fires` UNIQUE-claim pattern (exactly-once start), `provider_health`/`cannot_route`, journal
incognito handling, web ConfirmCard/PlanChecklist/RunChip/RunInspector/sw.js notice.
**New:** `tasks` table (0017) + proto Task model + task.* conversation-level events + REST/WS (list,
cancel, steer, confirm-proxy); tools `tasks.delegate` / `tasks.note` / `tasks.list` / amend/drop;
a RunKind (or flag) for task runs wired into priority/routing/budgets/ConversationKind; a core-side
**call-ended** client message (+ idle fallback); a capability/health context block; cross-conversation
confirmation; report writer into the asking chat; web TaskCard, Tasks chip, call-screen noted list.

## Constraints / debt in the way
1. No call lifecycle on core (hang-up vs drop indistinguishable).
2. `_UNATTENDED` makes confirmations pass silently — task runs must NOT be unattended in that sense.
3. No per-conversation serialization: a child run inside the asking chat would interleave with Arsen's
   next turn and pollute the history/prefix cache → task work belongs in its own (hidden, linked)
   conversation, with the card/report/confirm surfaced in the asking chat.
4. MAIL RunKind gap shows how many places a new kind touches.
5. Voice style default forbids "after the call"; stored copy overrides defaults.
6. Concurrency: 3 runs, 2 calls/endpoint; background runs start between call turns.
7. In-memory chains / voice asks lost on restart.
8. Interrupted mutating calls resume as "did it complete?" → card must explain.
9. Message has no metadata → card = separate task entity rendered by web.
10. Incognito must propagate into task conversations (journal, KG, sessions).
11. "Only announced tasks" must be enforced in code, not only prompt text.
