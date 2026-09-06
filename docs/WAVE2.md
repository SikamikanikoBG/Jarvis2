# Wave 2 — what V1 still does that V2 does not, and how it comes across

Rule for every line below: V2 is one platform with a handful of primitives —
**conversations/runs · messages · tools (MCP) · context blocks · schedules · boards/notes ·
knowledge graph · skills · settings**. A V1 feature is re-homed onto one of those or it is
dropped. Nothing gets its own subsystem, its own poller, its own config file. V1 had ~100 tool
modules, 16 services and 158 web routes; that is the patchwork we are not rebuilding.

Inventory taken 2026-09-06 from `R:\Projects\Jarvis` (tools/, jarvis/*_service.py,
remote_access/server.py routes, shared-brain tables). Status: **have** = in V2 now ·
**wave 2** = to build · **ask** = only if Arsen wants it · **drop** = cut by his scope decision.

## Inventory

| V1 | V2 home | Status |
|---|---|---|
| **Email & Outlook** | | |
| get_emails / search_emails / get_email_content / get_email_by_entry_id | host `outlook_list / search / read` | have |
| send_email, create_email_draft, approved-recipient policy | `outlook_send(draft)` + `settings.email` | have |
| schedule_draft_send ("send this draft at 9:00") | a **schedule** whose prompt sends the draft (`outlook_send(reply_to_entry_id)`) — no new mechanism | wave 2 (host: `outlook_send_draft(entry_id)`) |
| email thread view (`/api/email/thread`, `/api/email/conversation`) | host `outlook_thread(conversation_id)` → also the **email-thread attachment** | wave 2 · slice 1 |
| mark read / unflag / bulk actions | host `outlook_mark(entry_ids, read)`; flag exists | wave 2 · slice 2 |
| email attachments (download/read; V1 could not read them either) | host `outlook_attachments(entry_id)` + the document reader below | wave 2 · slice 2 |
| manage_email_alerts ("tell me when X writes") | a **triage category** with a notify action, or a scheduled prompt — no alert engine | wave 2 · slice 2 |
| search_emails_rag / deep_email_search | `outlook_search` is DASL + verified; RAG over mail needs an embedding index | ask |
| email accounts UI (`/api/email-accounts`) | `outlook_accounts` + `settings.triage.accounts` | have |
| background triage, demand routing, security quarantine | `TriageJob` (categories incl. `quarantine`) | have (off until go) |
| per-account folder structures (work vs personal Gmail) | `settings.triage.account_rules` + `outlook_folder_create` | have |
| meeting auto-RSVP | `RsvpJob` | have (off until go) |
| **Calendar** | | |
| get_calendar / create / delete / find_free_slots / check_availability | host `calendar_list / create / delete / free_slots / invites` | have |
| update_calendar_event (move/rename) | host `calendar_update(entry_id, …)` | wave 2 · slice 2 |
| create_multiple_calendar_events | model calls `calendar_create` N times; refusal on busy slots | have |
| **Tasks & contacts** | | |
| create_task / get_tasks / update_task (Outlook tasks) | host `tasks_list / tasks_create / tasks_update` | wave 2 · slice 2 |
| get_contacts / find_contact ("Rumen" → address) | host `contacts_find(query)` (GAL + Contacts) | wave 2 · slice 2 |
| **Documents & files** | | |
| read_file / write_shared_file / list/search shared files | host `fs_*` (text) | have |
| word_read / excel_read / pptx_read, PDF | one host `doc_read(path)` → Markdown for docx/xlsx/pptx/pdf | wave 2 · slice 1 (same reader serves attachments) |
| word_write / excel_write / pptx_write / doc_edit | host `doc_write(path, format, content)` | wave 2 · slice 2 |
| onenote_read / onenote_write | host `onenote_tree/read/search/create/append/move` | **done** (2026-09-06, live-verified) |
| meeting recorder (mic + system audio) | host `meeting_start/pull/stop` → core transcribes and summarises | **done** (2026-09-06, live-verified) |
| markdown_parser, paste_content, read_clipboard | attachments + host `clipboard_read` | wave 2 · slice 1 |
| **Images & vision** | | |
| image upload (`/api/images/upload`, thumbs), vision status | **attachments**: image → multimodal content part (vader's qwen3.8-27b answers on images — probed 2026-09-06) | wave 2 · slice 1 |
| generate_image / render_illustration | — | drop (Arsen's cut) |
| **Web** | | |
| web_search, browse_web, http_request, deep_search_tool | `web.search` (SearXNG), `fetch.fetch`; generic `web.request` for JSON APIs | have / wave 2 (small) |
| browser_automation / browser_panel | extension over `/ws` (`browser.*` tools) | have — untested in Brave |
| **Voice** | | |
| STT (mic, remote Whisper) | `/api/stt` → WhisperX on ardi | have |
| TTS (`/api/tts/speak`, per-conversation voice/speed) | `/api/tts` + a speaker button per reply + auto-speak toggle | ask (V1 was a *voice* assistant; not in the keep-list) |
| play_music | — | drop |
| **Chat features** | | |
| conversations CRUD, rename, archive | have (delete = "⋯" menu; a header menu is added in this wave so it is obvious on the phone) | have |
| per-conversation model / thinking / system prompt / custom instructions / memory space | per-message thinking exists; per-conversation **instructions + role override** as conversation fields, rendered into the stable prefix | wave 2 · slice 3 |
| message reactions, pins | notes boards (pin) — reactions dropped | drop |
| slash-command completions (`/api/completions`) | composer `/` menu over skills + schedules | wave 2 · slice 3 |
| input history | composer ↑ history (local) | wave 2 · slice 3 |
| TTS voice per conversation | see Voice | ask |
| **Knowledge & memory** | | |
| kg_query, knowledge graph, source routing | `kg.*` + `KnowledgeBlock` | have |
| manage_notes / boards | `notes.*` | have |
| save_idea / get_ideas | a board "Ideas" (data migration: V1 `ideas` table → notes) | wave 2 · slice 4 (data) |
| manage_projects (`/api/projects`) | KG entities of type `project` + a board; migration from V1 `projects` | wave 2 · slice 4 (data) |
| track_finance / finance_manager, track_wellbeing / wellbeing_manager | "Finance" and "wellbeing" as boards + KG facts, written by skills — no ledger subsystem | ask (which do you still use?) |
| manage_journal, todo_write | run plans + journal dropped; `jarvis.plan_step_done/replan` exist | have / drop |
| brain files / memories (`/api/brain/*`) | KG + shared folder via `fs_*` | have |
| **Automation** | | |
| reminders / scheduler / instances | schedules (one conversation per fire) | have |
| cases, escalation/proactive, growth/self-improvement, skills-audit | — | drop (Arsen's cut) |
| daily_brief, generate_management_report, meeting_prep_service | **scheduled prompts + skills** (a morning brief is a prompt at 07:30; meeting prep is a prompt that reads `calendar_invites`/`calendar_list` and mails you) | wave 2 · slice 4 (prompts, no code) |
| background processes UI, agent_task/agent_status, self_status | runs screen (`/runs`) | have |
| manage_settings (Jarvis edits its own settings) | `settings.*` tool over `PATCH /api/settings` | wave 2 · slice 3 |
| **System** | | |
| system_action, system_monitor, run_command, run_python, code_* | host `shell_run`, `fs_*`, `screen_grab`, `volume_*`, `host_status` | have (run_python = shell) |
| fleet_tools, distributed concierge | MCP servers list (each machine = one `jarvis-host`) | have |
| integrations catalog / toggle | `settings.mcp_servers` | have |
| logs console (`/api/logs`) | run events exist per run; a global **Logs** view (core log tail + host log) | wave 2 · slice 3 |
| home dashboard (`/api/home/*`: calendar, emails, tasks, weather) | a **Today** view for the phone (calendar_list + outlook_list unread + tasks) | wave 2 · slice 3 |
| **Messaging** | | |
| send_discord_message | `notify.discord` | have |
| send_whatsapp_message | — | ask |
| agent collab (`/collab`), collab keys | `/mcp` server + keys | have |

## Slices, in order

### Slice 1 — Attachments (photos, files, email threads, clipboard)
One concept, not four features. Stories in `docs/stories/09_attachments.md`.

- `attachments` table (id, conversation_id, message_id, kind image|file|email|page|text, name,
  mime, bytes, path, text, meta, created_at). Blobs on disk under `JARVIS_HOME/attachments/`.
- `POST /api/attachments` (multipart, ≤ 25 MB, chunked for the phone), `GET /api/attachments/{id}`
  (+ `?thumb=1` for images), `DELETE`.
- `run.create` carries `attachment_ids`; the user message stores them; the SPA composer has
  one "+" button: photo/camera, file, paste (clipboard image or text), **email thread** (picker
  over `outlook_search` → host `outlook_thread`), and the browser panel already sends the page.
- Ingestion is one function, `attachment_to_context(att)`: image → multimodal image part on the
  user message (vLLM/Ollama both accept it); pdf/docx/xlsx/pptx/txt/md/csv → text via `doc_read`
  (host, when the file is on a host; core-side for uploads) trimmed to a budget; email thread →
  the thread text; page → the existing browser context. Non-image text lands in the persisted
  `name="context"` message, so the prefix cache stays intact.
- Vision gate is structural: the role's adapter probes the model once with a 1×1 image; without
  vision an image attachment becomes "[image attached: name, 1.2 MB — this model cannot see
  images]" instead of a silent drop.
- Gallery: attachments render as thumbnails/chips on the message; click = full view.

### Slice 2 — Outlook and documents complete on the host
`contacts_find`, `tasks_*`, `calendar_update`, `outlook_thread`, `outlook_mark`,
`outlook_attachments`, `outlook_send_draft`, `doc_read`/`doc_write`. Each is a plain MCP tool
with the read/mutating/destructive annotation; nothing else changes.

### Slice 3 — Chat surface parity
Header "⋯" menu (rename/archive/delete/instructions), per-conversation instructions + role
override (stable prefix), composer `/` completions and ↑ history, Today view, Logs view,
`settings.*` self-edit tool.

### Slice 4 — Data and prompts
Ideas → board, projects → KG, V1 message attachments → attachments (files are in the shared
folder), the daily brief / management report / meeting prep as scheduled prompts.

### Decisions taken by Arsen (2026-09-06)
TTS **no** · WhatsApp **no** · finance / wellbeing trackers **no** · RAG over mail **no** ·
OneNote **yes** (host COM tools, slice 2). Plus: chat-surface parity with ChatGPT / claude.ai /
Open WebUI ("copy a message, share it…") — slice 0 below, built as ONE action bar, ONE menu,
ONE search, not a widget per feature.

### Slice 0 — Chat surface parity — **shipped 2026-09-06 (2.0.0a5)**
Audit of V2 on 2026-09-06: a bot message has one action (pin to board); a user message has
none; no code-block copy; no search; no export; no auto-title; no scroll-to-latest; no
shortcuts; no notification when a long run finishes in a background tab.

| They have | V2 home | In |
|---|---|---|
| copy message (markdown) · share (Web Share on the phone) | one `MessageActions` bar on every message | slice 0 |
| regenerate a reply | new run with the same input; the earlier reply stays visible | slice 0 |
| edit a message and resend (branch) | **fork**: `POST /api/conversations/{id}/fork?up_to=` copies the transcript up to that message into a new conversation, the edit is sent there — history stays append-only | slice 0 |
| copy button + language label on code blocks | markdown renderer | slice 0 |
| search conversations and messages | one `GET /api/search?q=` (titles + message text) + sidebar search box | slice 0 |
| pin / star a conversation | `pinned` on Conversation; pinned sort first | slice 0 |
| automatic conversation title after the first exchange | classifier role names it once (`title_auto`), rename turns it off | slice 0 |
| export a conversation (Markdown / JSON) | conversation menu → download | slice 0 |
| jump to latest + "new reply below" while scrolled up | transcript | slice 0 |
| keyboard shortcuts (new chat, search, stop, focus composer) | app shell, one map | slice 0 |
| desktop notification + tab badge when a run finishes in a hidden tab | app shell, `run.done` | slice 0 |
| attachments, paste, drag-drop, image lightbox | slice 1 | **next** |
| slash commands / prompt library, ↑ history, per-chat instructions & model | slice 3 | slice 3 |
| message feedback thumbs, temporary chat, canvas/artifacts, custom folders & tags, multi-model compare, read-aloud | — | drop |
