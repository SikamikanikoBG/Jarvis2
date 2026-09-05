# Jarvis V2 — API contracts (Phases 2–7)

Authoritative for anyone building against the core: the SPA, the Windows host daemon, the
browser extension, external collaborators. Types are the pydantic models in
`packages/proto` (mirrored in `web/src/protocol/types.ts`). Auth: `Authorization: Bearer
<token>` on REST, `?token=` on WS; when no token is configured everything is open.
Phase-1 contracts (conversations, runs, settings, tools, `/ws`) are unchanged and listed in
`web/README.md`.

Conventions: ids are strings (`brd_…`, `note_…`, `ent_…`, `sch_…`, `mtg_…`, `key_…`); times are
ISO-8601 UTC; every mutation is echoed on the WS as an event so all open clients converge;
`DELETE` returns 204; validation failures are 422 `{detail: [...]}`.

## Boards (Phase 2)

Sticky notes Arsen pins; every board is injected into the model's context, compactly.

```
GET    /api/boards                         → Board[]
POST   /api/boards        {name}           → Board                     201
PATCH  /api/boards/{id}   {name?, position?} → Board
DELETE /api/boards/{id}
GET    /api/boards/{id}/notes              → Note[]
POST   /api/boards/{id}/notes {text, color?, from_message_id?} → Note   201
PATCH  /api/notes/{id}    {text?, color?, board_id?, position?} → Note
DELETE /api/notes/{id}
```
`Board {id, name, position, note_count, created_at, updated_at}`
`Note {id, board_id, text, color: "yellow"|"blue"|"green"|"pink"|"grey", from_message_id, position, created_at, updated_at}`
WS: `board.changed {board_id}` (UI refetches that board; `board_id` null = list changed).
Tools (builtin): `notes.boards`, `notes.list {board}`, `notes.add {board, text, color?}`,
`notes.update {note_id, text?, color?}`, `notes.remove {note_id}`.

## Knowledge (Phase 2)

```
GET    /api/kg/entities?q=&limit=50        → Entity[]  (q matches name/alias, case-insensitive)
GET    /api/kg/entities/{id}               → EntityDetail
PATCH  /api/kg/entities/{id} {name?, type?, summary?} → Entity
DELETE /api/kg/entities/{id}
POST   /api/kg/entities/{id}/merge {into}  → Entity (aliases + edges move to `into`)
GET    /api/kg/graph?center=&depth=1&limit=80 → {nodes: Entity[], edges: Edge[]}
```
`Entity {id, name, type: "person"|"org"|"project"|"place"|"thing"|"topic", summary, aliases: string[], mention_count, updated_at}`
`Edge {src, dst, relation, weight, evidence}` · `EntityDetail = Entity & {edges: (Edge & {other: Entity})[], mentions: {conversation_id, message_id, snippet, at}[]}`
Learning is a background `system` run after each `run.done` (classifier role, thinking off).
WS: `kg.changed {entity_ids: string[]}`.
Tools (builtin): `kg.who_is {name}`, `kg.related_to {name}`, `kg.remember {name, type, summary, relations?: [{to, relation}]}`.

## Skills (Phase 2)

Markdown files with YAML frontmatter (`name`, `description`, optional `triggers: string[]`)
in `JARVIS_HOME/skills/`. Detection = one pre-flight classifier call over the index.

```
GET    /api/skills                → Skill[] {name, description, triggers, enabled, size, updated_at}
GET    /api/skills/{name}         → {name, content}
PUT    /api/skills/{name} {content} → Skill   (creates or replaces; frontmatter validated)
PATCH  /api/skills/{name} {enabled} → Skill
DELETE /api/skills/{name}
```
WS: `skills.changed {}`. Run events: `context.skills {names: string[]}` after detection.
Tool (builtin): `skills.use {name}` → the skill body.

## Plans (Phase 2)

Already in the protocol: `Run.plan`, `plan.created`, `plan.step_started`, `plan.step_done`.
The model advances its own plan with builtin tools `jarvis.plan_step_done {index, note?}` and
`jarvis.replan {goal, steps}`. The UI renders `Run.plan` as a checklist under the run chip.

## Compaction (Phase 2)

Server-side only. When a conversation's history exceeds the working budget, older turns are
summarised into `conversation_summaries(conversation_id, up_to_message_id, text)` and the
context is `[system, summary, messages after up_to]`. UI shows a thin "earlier messages
summarised" divider at `up_to_message_id` (`GET /api/conversations/{id}/summary`).

## Hosts = MCP servers (Phase 3)

There is no separate hosts table. A machine that offers capabilities runs `jarvis-host`,
an MCP server over streamable HTTP, and is listed in `Settings.mcp_servers` like any other
server (`{name: "laptop", transport: "streamable_http", url: "http://<laptop>:9030/mcp",
headers: {Authorization: "Bearer <host token>"}}`). Its tools appear as `laptop.outlook_list`
etc.; Status shows it as a provider. Per-host config lives on the host (`host.toml`).

`jarvis-host` tool surface (namespace = the server's name; tool names below):
```
outlook_accounts()                          → accounts/stores with default folders
outlook_folders(account)                    → folder tree (ids + names)
outlook_list(account, folder, since?, cursor?, limit=25)  → partial-aware listing (GetTable)
outlook_read(entry_id)                      → headers + body text (+ attachment names)
outlook_search(account, query, days_back=30, cursor?)
outlook_move(entry_id, folder)              [destructiveHint=false, readOnly=false]
outlook_flag(entry_id, flag: bool)
outlook_send(account, to, subject, body, cc?, reply_to_entry_id?)  [destructiveHint=true]
calendar_list(account, days=7)              calendar_create(...) [destructiveHint=true]
fs_list(path) fs_read(path) fs_write(path, text) [destructive] fs_search(root, glob)
shell_run(command, cwd?, timeout_s=60)      [destructiveHint=true]
screen_grab(monitor?) → PNG as MCP image content
meeting_start(device?) / meeting_stop() / meeting_status()   (Phase 6; streams to core)
```
Rules carried from V1 (non-negotiable): `Folder.GetTable` for listings; never trust
`FlagStatus` alone; Bulgarian Outlook → resolve default folders by `GetDefaultFolder` id, not
by name; table EntryIDs are short-term — re-resolve before mutating; one COM thread, per-call
timeouts, no zombie waiters.

## Browser extension (Phase 5)

The extension connects to the same `/ws` with `?client=browser&token=…` and is a tool
provider (`WsProvider`, namespace `browser`):

```
ext → core  browser.hello   {agent, version, tools: ToolSpec[]}      (on connect; registers tools)
core → ext  browser.call    {call_id, name, arguments}
ext → core  browser.result  {call_id, kind: "data"|"empty"|"error", text, error?}
ext → core  browser.context {url, title, selection?}                (on tab change; optional)
both        ping / pong
```
Tools the extension exposes (V1 pageKernel, ported): `browser.tabs`, `browser.open {url}`,
`browser.read {tab?, mode: "text"|"outline"}`, `browser.find {query}`, `browser.click {ref}`,
`browser.type {ref, text, submit?}`, `browser.scroll {ref?|direction}`, `browser.screenshot`.
`browser.context` feeds a context provider ("the page Arsen is looking at") for runs created
from the side panel. The side panel is the SPA in an iframe: `/?mode=panel&token=…`.

## Schedules (Phase 4)

Replaces V1 reminders/scheduler. Each fire = a new conversation (`kind=scheduled`,
`folder_key=schedule.id`, `folder_label=schedule.name`, title `"<name> · <YYYY-MM-DD HH:mm>"`) + one run.

```
GET    /api/schedules                → Schedule[]
POST   /api/schedules {name, prompt, cron?, at?, tz?, enabled?, catch_up?, think?, think_level?} → Schedule 201
PATCH  /api/schedules/{id} {…same…}  → Schedule
DELETE /api/schedules/{id}
POST   /api/schedules/{id}/run       → {run_id, conversation_id}   (fires now)
GET    /api/schedules/{id}/fires?limit=20 → Fire[] {scheduled_for, run_id, conversation_id, status}
```
`Schedule {id, name, prompt, cron: string|null, at: datetime|null, tz, enabled, catch_up: "skip"|"run_once",
think, think_level, next_fire, last_fired_for, last_run_id, last_status, created_at, updated_at}`
Exactly one of `cron` / `at` (one-shot, auto-disables after firing). `UNIQUE(schedule_id,
scheduled_for)` makes double-fire impossible. WS: `schedule.changed {schedule_id|null}`.
Tools (builtin): `schedule.create {name, prompt, cron?, at?}`, `schedule.list`, `schedule.delete {id}`.
Sidebar: `Scheduled ▸ <schedule name> ▸ conversations by date`.

## Triage (Phase 4)

Built-in system schedule (`triage`, every `settings.triage.interval_min`) → run kind
`triage` in the day's conversation per account (`kind=triage`, `folder_key=account`,
`folder_label=account`, title `YYYY-MM-DD`). Zero-LLM cursor poll via the host's
`outlook_list(since=cursor)`; triage role classifies (thinking off); demand routing first
(a `DM-1234` literally present → `Demands/DM-1234`), model for the rest; `outlook_move`.

```
GET  /api/triage/state                         → [{account, cursor, last_run_at, processed_today, routed_today, errors}]
POST /api/triage/run                           → {processed, routed, accounts, errors, proposed: []}
POST /api/triage/run?dry_run=true              → same shape; nothing moved/recorded/advanced,
                                                 proposed: [{account, sender, subject, category, folder, current_folder}]
POST /api/triage/run?dry_run=true&folder=F&limit=N
                                               → samples the newest N mails of an ALREADY-SORTED
                                                 folder: proposal vs current_folder = accuracy check
                                                 (422 without dry_run)
```
Settings: `triage {enabled, interval_min, host, accounts: string[], demand_root: "Demands",
demand_prefixes: ["DM-"], categories: [{name, folder, rule}], instructions: str,
fallback_category: str}`. `instructions` is free text the classifier reads before the categories
(owner, what Cc-only means, VIP list, hard exclusions); `fallback_category` is where "none"
lands (set it to the catch-all for inbox zero). The classifier sees `From: name <smtp>`, `To`,
`Cc`, subject and a 500-char preview; the host list is asked for `preview_chars=1500` so the
demand rule can tell a digest from a thread. Demand routing: an id in the SUBJECT wins; a
body-only match counts only when the body names exactly one distinct demand. `outlook_move` is
called with `create=true` so a first mail about `DM-2300` makes its folder. Decisions persisted in
`triage_decisions(entry_id, account, category, action, run_id, at)` so a crash re-does nothing.
`scripts/triage_eval.py` drives the dry runs and prints agreement per folder.

## Meeting auto-RSVP (docs/stories/08)

Background job (`rsvp`, every `settings.rsvp.interval_min`) over the host's calendar tools:
`calendar_remove_canceled` → `calendar_invites` (pending, each with its COMMITTED clashes) →
per occurrence not in the ledger: outside `allowed_domains` and not VIP → left; free → accept;
VIP + clash → accept and say so; clash → decline with up to `propose_slots` alternatives from
`calendar_free_slots` (spread ≥ 2 h apart). Ledger key `organizer|subject|start`
(`rsvp_decisions`), so a re-surfaced recurring occurrence is answered once. Every pass with
decisions appends one line each to the day's "Calendar RSVP" conversation.

```
GET  /api/rsvp/state              → {enabled, state: {account, last_run_at, last_error, answered_total,
                                     removed_canceled_total} | null, recent: [decisions]}
POST /api/rsvp/run                → {pending, removed_canceled, errors, decisions: [RsvpDecision]}
POST /api/rsvp/run?dry_run=true   → same decisions, nothing sent, nothing recorded
```
Settings: `rsvp {enabled, interval_min, host, account, lookahead_days, allowed_domains: string[],
vip: string[] (addresses or domains), remove_canceled, propose_slots, work_start_hour,
work_end_hour}`. An empty `allowed_domains` answers nobody (fail closed).

Host tools behind it: `calendar_invites(account, days)`, `calendar_respond(entry_id, decision,
comment, account)`, `calendar_free_slots(account, start, days, duration_min, work_start_hour,
work_end_hour, limit)`, `calendar_remove_canceled(account, days_back, days_ahead)`.

## STT (Phase 6)

```
POST /api/stt  multipart: audio (webm/ogg/wav), language? → {text, language, backend, duration_ms}
```
Core forwards to `settings.stt_url` (the Whisper service). No fallback: a failing backend is a
`502 {detail}` and a red badge, never a different model.

## Meetings (Phase 6)

```
POST   /api/meetings {title?, host}      → Meeting (starts capture via <host>.meeting_start)   201
POST   /api/meetings/{id}/stop           → Meeting (triggers the summary run)
GET    /api/meetings                     → Meeting[]
GET    /api/meetings/{id}                → MeetingDetail {…, segments: [{t0, t1, text}], frames: [{at, url, ocr}]}
POST   /api/meetings/{id}/chunks         (host → core) multipart audio chunk + t0
POST   /api/meetings/{id}/frames         (host → core) multipart PNG + at
```
`Meeting {id, conversation_id, title, host, status: "recording"|"summarising"|"done"|"failed", started_at, ended_at}`
WS: `meeting.segment {meeting_id, t0, t1, text}`, `meeting.changed {meeting}`. The transcript
lives in a `kind=meeting` conversation; the summary is a `meeting` run in it.

## Collab (Phase 7)

Core as an MCP server at `/mcp` (streamable HTTP, bearer key per collaborator) with tools
`jarvis_chat {message, conversation_id?} → {reply, conversation_id, run_id}`, `jarvis_status`,
`jarvis_notes {op, …}`. Also `POST /api/collab/message {text, conversation_id?}` → same shape.
Each key owns its `kind=collab` conversations (`folder_key=key.id`, `folder_label=key.name`).

```
GET    /api/collab/keys            → Key[] {id, name, created_at, last_used_at}   (owner only)
POST   /api/collab/keys {name}     → {key (shown once), id, name}
DELETE /api/collab/keys/{id}
```

## Pairing (Phase 7)

`GET /api/pair` (owner) → `{url, qr_svg}` for the phone. `GET /api/whoami` → `{owner: bool, key_name?}`.

## Import from V1 (Phase 7)

`uv run python scripts/import_v1.py --db R:\Projects\Jarvis\jarvis.db --skills R:\Projects\Jarvis\skills`
imports boards + notes, KG (entities/aliases/edges/mentions), skills files, schedules
(V1 `scheduler_jobs` → cron), and conversations as `kind=archive` (read-only folder
"Archive"). Idempotent (V1 ids kept in an `import_map` table).
