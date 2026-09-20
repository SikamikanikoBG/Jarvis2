# Jarvis V2 — design journal

Started 2026-09-05. Purpose: record the before-state of V1 and the decisions taken while
designing V2, so the reasoning survives the rewrite.

## Before-state (V1 at commit 3b3cb57, version 1.310.0)

- ~1,554 commits since 2025-12-25 (about eight months of accretion).
- `jarvis/` = **114,438 LOC Python**, of which `textual_ui/` = 19,345. `tests/` = 46,861.
- Web UI = **one file**, `jarvis/remote_access/web_client.html`, 15,542 lines (HTML+CSS+JS).
- Browser extension = `extension/background.js` 3,173 lines + content/sidepanel scripts.
- `config/settings.json` = **110 top-level keys**; 13 backup copies of it sit next to it.
- `uv.lock` = **205 locked packages** (LangChain + LangGraph + 7 cloud provider SDKs,
  Textual, TTS stacks, RAG stack, Playwright, Langfuse...).
- Three separate "one brain across machines" mechanisms coexist: `memory_service/`
  (SQL proxy), `jarvis/config_sync.py` (shared_config table), `jarvis/fleet/*`
  (concierge, unmerged branch).
- Background work runs through at least five different hand-off paths: scheduler thread,
  `text_input_queue`, `CaseService` poller, collab handler, `agent_gate` semaphore.
- CI deploys on every push to main but **never runs pytest**.

## Scope decision (2026-09-05, with Arsen)

Keep: Ollama + vLLM inference only; notes boards; knowledge graph; tools; plan-then-execute;
web UI; browser side panel; STT; meetings (recorder + screen capture); multi-inbox;
demand auto-routing; background triage; multi-host; remote memory; scheduled prompts
(replaces reminders/scheduler: each fire = its own conversation, grouped in a folder in
the sidebar); config sync; agent collab; MCP.

Cut: TUI; cloud LLM providers; cases; journal lanes; grounding gate; self-improvement
loop; benchmark framework; image generation / vision; setup wizard; PyPI packaging;
Langfuse; HomeLab Monitor integration.

Requirements stated: super fast backend; web UI only; "super efficient coding"; an agent
capable of long-running tasks; reuse the web UI if it is worth reusing; a *really robust*
harness — library vs. own is an open question to settle in the design.

## Log

- 2026-09-05 — audits of the web UI and the agent core launched; design doc drafted in
  `docs/v2/DESIGN.md`.
- 2026-09-05 — Arsen: "i want super robust AI agent harness (think claude code) and excellent
  UI. go - start building". Decisions in DESIGN.md §16 taken on the spot; new repo `Jarvis2`.
- 2026-09-05 — Audits: web UI = 11.6k vanilla JS, 213 globals, 275 inline handlers, 29 real
  browser tests (email only) → rebuild, salvage CSS. Agent core = 3,175-line `run_agent()`
  around LangGraph, 89 threads, tools 37k LOC at repo root → own the harness.
- 2026-09-05 — Phase 1 core green: 44 tests (cancel during stream/tool/queue, restart during
  read-only tool → resumes, restart during mutating tool → waits for Arsen, confirmation that
  survives a restart, budgets, judge stop/nudge/unavailable, error streak, idempotency, WS
  round-trip, token auth). pyright strict clean. Five harness bugs found by the tests on day
  one: `run.queued` published before the WS subscribed to a new conversation; `run.updated`
  carried a mutable Run; `last_seq` lagged the events table so a resume collided; a run parked
  in `waiting_user` was marked interrupted on shutdown; messages written in the same
  millisecond sorted randomly.
- 2026-09-05 — Real model (vader vLLM `qwen3.8-27b`): endpoint probe 276 ms; plain reply TTFT
  651 ms with 45 reasoning deltas (thinking via `chat_template_kwargs.enable_thinking`); the
  model chose `jarvis.time` on its own and answered with the correct Sofia time; Stop landed
  in 30 ms, partial text kept; run events persisted without deltas. `scripts/smoke.py`.
- 2026-09-05 — Repo pushed: github.com/SikamikanikoBG/Jarvis2 (private).
- 2026-09-05 — Arsen: "exactly the direction" + two asks: tools wired, thinking on/off with
  level. Done the same day: `McpProvider` (stdio + streamable HTTP, one session per server,
  annotations → policy), defaults `fetch` + ardi homelab MCP (20 tools); `think_level` per role
  (Ollama string levels / vLLM `reasoning_effort`) and per-message override on `run.create`.
  53 tests. Live vs vader vLLM: model chose `homelab.get_host` (accurate GPU report) and
  `fetch.fetch` ("Example Domain") out of 22 flat tools. Cost signal: 22 tool schemas ≈ +6k
  prompt tokens per call (9,479 vs ~600) — derived facades (DESIGN §6) are the Phase 3 answer.
- 2026-09-05 — Paused (Arsen closing the laptop). SPA agent's second pass (thinking chip,
  MCP-server editor, tools on Status) still running at pause; `web/` committed as it stood.
  Core left running locally on :9020 (token `smoke`, data in ./data), pointed at vader vLLM.
  Resume: `uv run jarvis-core` + `cd web && npm run build`; next = finish SPA pass, derived
  facades (§6), OpenAI-compatible `/v1/chat/completions` façade, then Phase 2 (boards/KG/skills).
- 2026-09-05 (later) — Arsen: "continue all phases". API contracts written (docs/API.md); three
  subagents in parallel: host daemon (Outlook COM MCP server), extension port, SPA third pass.
  Core: Phase 2 (boards, KG + background learner, skills, compaction, planner with engine-owned
  plan tools), Phase 4 engine side (schedules with UNIQUE slot claim), browser WsProvider,
  derived facades. Rate limit killed all three subagents mid-flight; resumed from transcripts.
- 2026-09-05 — Facades measured with qwen3.8-27b: flat = 1 call / 9.5k prompt tokens; facade
  (20-op homelab) = 7 calls / 12.1k tokens, model flailed on op+args. Default set to flat,
  facades kept as a setting. `fetch` and `notes.add` correct either way.
- 2026-09-05 — jarvis-host verified against the REAL Outlook on workocholic: 7 stores found,
  default = the Bulgarian one (`Входящи` resolved by GetDefaultFolder id, never by name);
  GetTable listing 71-86 ms, read 721 ms with 3 attachments, 45 calendar events 1.8 s.
  Two real defects found by that run: (1) the core's triage read `item["sender"]`/`["preview"]`
  while the host sends `from: {name,address}` and no preview — triage would have classified
  every mail with an empty sender and body; (2) Outlook's table CAN return the body
  (`urn:schemas:httpmail:textdescription`, verified live), so a preview column now ships with
  the listing and triage needs no per-mail COM read.
- 2026-09-05 — End-to-end with qwen3.8-27b over 52 flat tools: "what is in my inbox" →
  `workocholic.outlook_list` → correct 2 messages; "meetings in the next 3 days" →
  `workocholic.calendar_list` → correct, including a canceled meeting and a Bulgarian title.
  ~10k prompt tokens per call at 52 tools.
- 2026-09-05 — Phase 7: deployed to ardi (`scripts/deploy_ardi.sh`, container `jarvis2-core`
  on 100.97.120.53:9020, V1's jarvis-server on 9010 untouched). Configured for vader vLLM,
  the ardi homelab MCP and this laptop's jarvis-host over Tailscale — 52 tools, all green
  except the browser extension (Arsen loads that by hand). Fixed a 421 that blocked every
  remote core: the MCP SDK's DNS-rebinding guard trusts localhost only and matches no bare "*".
- 2026-09-05 — V1 import into ardi (core stopped, one-off container on the same volume):
  10 boards / 19 notes, 1,723 entities + 1,261 edges + 5,000 mentions, 40 skills, 49 of 50
  schedules (the 50th is malformed V1 data — reported, not guessed), 20 conversations / 578
  messages under an "Archive (V1)" folder. Schedules imported DISABLED on purpose: 13 were
  live in V1 and a data migration should not start firing automations unattended.
  V1's `days_of_week` is Python's Mon=0; cron's is Sun=0 — converted.
- 2026-09-05 — Proof the imported context reaches the model: "Who is Rumen?" answered
  correctly with ZERO tool calls (knowledge graph injected), "What is on my to-do board?"
  read the real notes via notes.list.
- 2026-09-05 — Real failure from Arsen's own use: "сложи звукът на 33%" made the model improvise
  PowerShell `Add-Type` p/invoke, fail, and retry the same broken script six times, each one
  stopping for a confirmation, until the supervisor stopped the run. Three separate defects:
  (1) no volume tool existed, so the model had only `shell_run`; (2) V1's `audio_devices` skill
  covers switching devices, not setting a level; (3) every attempt asked for confirmation, which
  turned a failure into six interruptions. The supervisor itself worked exactly as designed.
  Fixes: ported V1's Core Audio volume control into the host as `volume_get`/`volume_set`
  (mutating, not destructive — trivially reversible, so it does not stop an interactive run),
  declared the `comtypes` dependency it needs, and added `Settings.confirmations`
  (mode destructive|off, always_allow / always_ask patterns). Re-ran the same sentence: one
  tool call, 2 steps, "Готово, звукът е на 33%" — and the machine really is at 33%.
  Note: unattended runs (scheduled/triage/meeting/system) never asked for confirmation even
  before this — scheduled prompts were never blocked by it.
- 2026-09-05 (evening) — Schedule audit, one by one, from Arsen's 13 live V1 schedules. Method
  after the Burnout incident: `scripts/run_schedule.py --dry-run` (interactive run with the
  scheduled budget; every destructive step asks and is rejected) before any live fire.
  Results and what each one taught:
  * Wakeup / Sleeping sound — work (volume_set/volume_get; read-back proves the level).
  * Burnout Prevention — LIVE run stacked 32 placeholder blocks on a calendar that already
    held V1's 26. All 32 deleted by entry_id. Fixes: calendar_create refuses busy slots
    (structural), calendar_delete, planner gets tool names (it had planned a "recolour
    categories" step no tool could do and reached for shell_run), prompt rewritten to check
    before creating. Dry-run now proposes 1 DECOMPRESS + 1 FOCUS per day; 217k -> 150k tokens.
  * Daily Approvals — "Remind Arsen..." created a SECOND schedule: nothing told the model it
    WAS the schedule. Fix: "## This run" framing for scheduled runs; schedule.create/delete
    are destructive (ask); notify.discord so a reminder reaches the phone. Dry-run: 2 calls,
    23k tokens, Discord ping sent.
  * Jira summary — first blanket tool-result truncation made it re-read mails until the
    supervisor stopped it; truncation is now budget-based (oldest first, 40k tokens). Prompt
    rewritten for V2 tools (previews first, outlook_read max_chars=4000). Works.
  * Weekly Workload — every tool call was cut at 120 s; the report script takes 853 s.
    Core honours the tool's own timeout_s up to tool_timeout_max_s (1200); host fs roots
    now include R:\Documents\SharedFolderAI. Prompt passes timeout_s=1000. Re-test pending.
  * World news / AI Newsletter — fetch respects robots.txt (Google News, Reuters, Register
    blocked); V1 searched via SearXNG through curl. Added web.search (SearXNG on ardi:8085).
    Both then work; the newsletter legitimately needs ~650k tokens -> scheduled budget 1.2M.
  * Weekly priorities digest — guessed folder names (4 errors, judge nudged), 683k tokens.
    Prompt now: outlook_folders once, one bounded search per priority.
  * Context Enrichment — V1's manage_project is gone; rewritten to kg.remember/notes.add.
    Its dry-run failed only because the host was being restarted at that moment.
  * email_triage_audit, OneNote, homelab-monitor GitHub — depend on V1 tools that do not
    exist (manage_email_triage, OneNote COM, gh) or post publicly; left disabled for Arsen.
  Cross-cutting: TTFT 18 s on 34k tokens = zero prefix-cache benefit although vLLM reports
  84% hits server-wide. Cause: KG/skills blocks changed per message inside the system prompt.
  Restructured: stable system prefix; per-turn context persisted as a user message named
  "context" after the input; plan progress as an ephemeral trailer. Measurement pending
  (tailnet relay flapping from the laptop tonight).
- 2026-09-06 (night) — Prefix cache measured on vader: cold 12.9k tok / 4.9 s, then 16.2k /
  0.64 s and 20.1k / 0.45 s in the same conversation (`scripts/cache_probe.py`). Weekly
  Workload live (273 s, 7 calls, the 853-s script ran under timeout_s=1000) and the priorities
  digest dry-run (219 s, 0 errors, would send to Arsen only) both clean; 10 V1 schedules enabled.
  Triage against the REAL mailbox, dry-run only (`?dry_run=true`, plus `folder=` sampling of
  already-sorted folders — the accuracy check). Three rounds, 100 → 90 mails each:
  * Round 1: 33% agreement with V1's folders. Real defects: the classifier saw display names,
    not addresses (VIP list is addresses); "none" left mail in the inbox; Jira digests naming
    many DM-ids were filed under the first one; automated mail went to Reference instead of its
    topical folder. Also V1's own errors surfaced: auto-replies and "Accepted:" in Bosses,
    hAIper daily summaries in Quarantine (185/month — the #1 folder in the workload report).
  * Fixes: `From: name <smtp>` (new table column PR_SENDER_SMTP — `SenderEmailAddress` is an
    X500 DN for internal senders), To/Cc columns, `fallback_category`, `instructions` block
    ported from V1's classification_instruction, topical-first rule for system mail, digest rule
    (subject wins; body-only match must name ONE demand — and the body is READ for that, the
    table preview is too short), `outlook_move(create=true)` for a first mail about a demand.
  * Round 3: Reference 90%, AI 80%, RPA 70% (from 10%), Bosses 3/15 where 7 of the 12 misses
    are V1 breaking Arsen's own rules and 5 are VIP threads with Arsen in Cc. To-Do 5/20 is the
    open judgement call (V1 filed 4,337 mails there against a "be very selective" rule) — the
    20 samples are in the report for Arsen to calibrate. Left `enabled: false`: it moves mail.
  Restart lesson: Stop-ScheduledTask never ended the detached daemon, so two "host restarts"
  changed nothing and one evaluation ran without To/Cc — `scripts/restart-host.ps1` stops the
  ONE pid on :9030 after proving it is jarvis-host.
  Meeting auto-RSVP (story 08) built and dry-run against the real calendar: 9 pending invites
  in 14 days, 3 in the 5-day window → ExCo weekly from a VIP clashes with 3 blocks → accept +
  flag; CIR Forum clashes → decline with 3 alternatives; BNT coordination free → accept. Host
  gained calendar_invites / calendar_respond / calendar_free_slots / calendar_remove_canceled
  (committed-only conflicts: tentative and unanswered invites never block). Left
  `enabled: false`: it sends responses to colleagues.
- 2026-09-06 (morning) — Arsen's second pass. "/ws did not open" was the container restart during a
  deploy: probed afterwards, WS answers on the Tailscale IP in 125 ms and through the HTTPS serve
  path in 688 ms (certificate valid to 2026-10-19). V1 conversations: the first import came from
  the laptop's V1 db copy; the shared brain held one more with messages (Digital Safari). The
  importer gained `--only-conversations`, a title-based dedup (V1 lived in several db copies) and
  an exclude list; General + Background Tasks were removed at Arsen's request → 19 archived V1
  conversations. Delete existed only in the sidebar row's ⋯ menu, invisible enough that Arsen
  thought it was missing: a top-bar ConversationMenu (rename / archive / delete) sits next to the
  title on desktop and phone. Vision probed on vader's qwen3.8-27b (a 2×2 red PNG → "Red"), so
  attachments can carry images natively. docs/WAVE2.md maps V1's ~100 tools / 16 services /
  158 routes onto V2's primitives with a status per line and four slices; slice 1 is
  docs/stories/09_attachments.md. Chrome extension for a real-browser UI check was not connected.
- 2026-09-06 (day) — Arsen's UI verdict: "copy a message, share it… see what Open WebUI / ChatGPT /
  claude.ai have that I do not — just do not end up a patchwork like before". Audited V2's chat
  surface against the three, wrote the gap table into docs/WAVE2.md as slice 0 and built it as ONE
  action bar, ONE menu, ONE search: copy/share on every message, edit-and-resend (a FORK, because
  the history is append-only: POST /api/conversations/{id}/fork), regenerate the last reply, code
  blocks with a language label and a copy control, GET /api/search over titles + message text
  (injected context messages excluded), pinned chats, export to Markdown/JSON, automatic chat
  titles after the first exchange (classifier role, a rename turns it off for good), tab badge and
  an opt-in desktop notification when a run finishes in a hidden tab, and four shortcuts
  (Ctrl/Cmd+Shift+O, Ctrl/Cmd+K, Esc, Shift+Esc). Dropped deliberately: feedback thumbs, temporary
  chats, canvas, tags, multi-model compare, read-aloud. Arsen's decisions on the rest of wave 2:
  TTS no, WhatsApp no, finance/wellbeing no, RAG over mail no, OneNote yes.
  Also: his Gmail (an IMAP store inside the same Outlook) was never triaged, because triage had ONE
  category set. Now `triage.account_rules` holds a rule set per mailbox (categories, instructions,
  catch-all, demand routing on/off) and the host gained `outlook_folder_create`, so a mailbox that
  lacks the folders gets them once before the first move.
- 2026-09-06 (afternoon) — Gmail triage, second round: 64% → 84% agreement on 68 real mails
  (Newsletters and Notifications 100%, Finance 92%, Shopping 80%, Personal and Action 67%). The
  first round's misses were all rule defects, not model failures: LinkedIn "X just messaged you"
  is a human reaching him (personal), security alerts about his own repos and credentials are
  action, a subscribed publication is a newsletter whatever its tone, hotel bookings are
  shopping, and platform statements (IUVO, Klear) are finance. Also learned: a redirected stdout
  buffers 8 kB, so a healthy 10-minute evaluation looked hung and I killed it — both scripts are
  line-buffered now, and restarting the host under a running evaluation stalls it.
  Arsen: "мигрирай и VIP имейлите — да ме алармира". V1 kept five alert rules in
  config/alerts_config.json (Rumen Radushev, Petya Dimitrova, Todor Plugchiev, Diana Dimitrova,
  Yordanka Kostova). Ported as `TriageAlert` on the triage rules: the match is STRUCTURAL (address,
  domain, subject substring), so a VIP mail cannot be missed because a classifier had an opinion;
  filing is unchanged; one Discord push per pass, not per mail; the day's triage message marks the
  lines [ALERT name]; a dry run reports them and sends nothing.
- 2026-09-06 (late) — Meeting notes: the cause was blunt. The core has called
  <host>.meeting_start / meeting_pull / meeting_stop since phase 6 and the host implemented none
  of them, so every host in the dropdown failed. Built the capture (mic + WASAPI loopback, mixed
  to 16 kHz mono in the host with a carried resampler state), and the first live run exposed three
  more defects, each now covered by a test:
  * a burst under a second was drained from the device and thrown away — the opening words before
    the first pull were lost;
  * the core kept a stale tool index: a restarted host that gains tools stays invisible ("unknown
    tool 'workocholic.meeting_start'") until a manual /api/tools/reload. Providers now re-list
    themselves on every reconnect, and the core probes each MCP server every 30 s — checking
    `connected` was not enough, because the client keeps a session the server has already
    forgotten, so a failed probe now drops the dead session;
  * Whisper answers silence with invented words: the first meeting transcribed a quiet room as two
    segments of "Thank you.", and with ZERO segments the summary run cheerfully summarised an
    unrelated Teams call from August. Silence is dropped before transcription (the clock keeps
    running so later offsets stay true), and a meeting with no segments never starts a summary run
    at all — it says what to check instead.
  Also: `jarvis_host.__version__` was a second literal that had drifted five releases behind its
  pyproject (the host reported 2.0.0a1 while running a6); both packages read their version from
  the installed metadata now. `uv sync` cannot replace Scripts\jarvis-host.exe while the daemon
  runs and leaves the venv without jarvis_core when it fails half-way — `restart-host.ps1 -Sync`
  does it in the window where the host is down.
- 2026-09-06 (evening) — Why the meeting transcript was empty, chased to the bottom. The capture
  opened both devices and delivered 1.5 MB of frames whose every sample was zero; a minimal
  blocking read straight from pyaudiowpatch did the same; and the render endpoint's own peak meter
  (IAudioMeterInformation) read 0.0 while Windows' voice spoke for three seconds. So nothing is
  rendered or captured in this process context on this laptop — the recorder is innocent, and the
  fix is diagnostics, not audio code: the host measures each source's peak, meeting_start listens
  half a second before answering, and start/pull/stop carry `levels` + a plain-language `warning`
  which the core writes into the meeting conversation at the start and repeats when a recording
  ends with nothing. Verified live: both sources report level 0 with the warning naming each one.
  Arsen still has to check Windows microphone access for the host and where his sound is going.
- 2026-09-06 (evening) — Attachments (wave-2 slice 1) built: one `attachments` table, one upload
  endpoint and one ingestion path for photos, documents, pasted text and (next) email threads.
  Images are resized to 1568 px on the way in (a phone photo is 4000 px and the model reads none
  of it) and reach the model as image parts — OpenAI-style content parts for vLLM, bare base64 in
  `images` for Ollama; documents become text that is inlined into the message the model sees, so a
  PDF works even on a model that cannot see pictures. A scanned PDF says it has no text layer
  instead of attaching nothing. UI: a paperclip menu (Take a photo on a phone, photo or file
  otherwise), drag-and-drop, paste (a screenshot becomes an attachment, and pasted text over 2 kB
  becomes one instead of filling the input), chips and thumbnails on the sent message, a lightbox
  with download. The `data_url` an image is sent with is filled in only while building the model
  request and is excluded from serialisation, so nothing persisted or broadcast carries base64.
  Meetings can be deleted now too (rows, frames on disk, and the transcript conversation).
- 2026-09-08 — Three things the chat list could not do. **Multi-select**: a Select button in the
  sidebar head (ctrl-click or the row menu gets you there too, shift-click takes a range), a bar
  that says how many are ticked, and one request for the whole gesture —
  `POST /api/conversations/bulk {ids, action}` covers delete, archive, unarchive, move, mark read
  and pin, de-duplicates the ids and skips the ones that are already gone, so clearing out thirty
  dead chats is one round trip and one round of events rather than thirty of each. **Folders Arsen
  names himself**: a `conversation_folders` table and a nullable `conversations.folder_id`,
  deliberately separate from the `folder_key`/`folder_label` pair the *machine* fills in for
  schedules, triage accounts and collab keys. Filed chats leave the flat list for their folder;
  archiving still wins over filing, so unarchiving puts a chat straight back where he had it; and
  deleting a folder never deletes the chats in it (`ON DELETE SET NULL` — they land back in the
  flat list). Rename and delete from the folder's own menu, file a chat from the row menu, or drag
  it — dragging a row that is part of a selection carries the whole selection, and dropping on the
  list itself takes chats out of every folder. Collapse state is remembered per device.
  **The activity dot**: `Conversation.activity` (`idle | running | waiting`) is derived from the
  runs table in the same SELECT that reads the conversation, never stored — a second copy of a
  run's status is the thing that goes stale. The sidebar could not have computed this itself: a
  client loads runs for the conversation it is looking at and no others, which is exactly the set
  the dot is not about. `conversation.updated` is conversation-level and reaches every client, and
  the engine already published one when a run is created and when it ends; the two transitions in
  between (parking on a confirmation, being let go again) now announce too. A green ring that
  breathes for working, a steady amber one for "will not move until you answer" — amber wins, and
  a collapsed folder shows the strongest state of what is inside it. Deliberately not the same
  shape as the unread dot: that one means something happened, this one means something is
  happening.
  One real bug found by driving it: a folder sits inside the list, the list accepts drops too (that
  is how a chat leaves every folder), so filing a chat by dropping it on a folder instantly un-filed
  it as the event bubbled. Also fixed while in there: on a touch device the row menu has no hover to
  reveal it, so it was printing on top of the timestamp.
- 2026-09-08 (later) — And a filter to go with the dot: one toggle in the sidebar head, with a
  badge saying how many chats are live, that swaps the whole grouped list for a flat list of
  what is running. Flat on purpose — the view answers one question, "what is Jarvis doing right
  now", and a scheduled fire that is working belongs in that answer next to a chat rather than
  behind a folder head that has to be opened first; filing is not the point there. Purely
  client-side (it filters on `activity`, no endpoint), and session-only rather than remembered:
  a "what is happening" view restored on a quiet morning would greet him with an empty list and
  look like every chat had gone. Empty is a normal state for it, so it says "Nothing is running"
  and offers the way back. Two things caught while driving it: the toggle had been renaming
  itself to "Show all chats" when active, colliding with the empty state's own button of that
  name — a toggle keeps one name and says its state through `aria-pressed`; and dropping a chat
  on the flat list un-files it, which is right when that list means "no folder" and wrong while
  it means "the live ones", so the root drop is off while filtering.
- 2026-09-09 — The filter now covers the blue dot too, because running and unread are the same
  chat five seconds apart: a run finishes, the reply lands, and having to leave the filter to go
  and read the answer was the whole reason for turning it on. So the predicate is
  `activity != idle || unread`, the toggle is "only what's waiting for me", and its badge counts
  the union. One thing that only shows up when you use it: opening an unread chat marks it read,
  which would drop the row out from under the cursor while reading it — so the conversation on
  screen is pinned into the filtered list (and deliberately not counted in the badge).
  Search, meanwhile, already existed and was quietly half-wired. Titles were matched locally over
  the conversations the client happened to have loaded, and the server's answer was filtered down
  to `matched === 'message'` — so its title half, the only thing that can find an ARCHIVED chat by
  name, was thrown away. Both halves are used now, de-duplicated by id. A chat hit renders as a
  real conversation row (preview, time, unread and activity dots, its menu) instead of a bare
  line; a message hit highlights the query inside the snippet with `<mark>`; and clicking one
  lands on the message that matched instead of at the bottom of the transcript — messages carry
  `id="msg-<id>"` now, and the transcript unpins itself, scrolls the match into the middle and
  flashes it for a moment, with "Jump to latest" left offered. The effect re-runs on each items
  change, so it waits out the fetch rather than guessing a delay.
  And the mock core had no `/api/search` at all: in dev the box could only ever do local title
  matching, which is a good part of why the feature felt missing. It has the route now, matching
  the core's rule (titles, then user/assistant messages that are not injected context, one hit per
  conversation, a ~70-character window around the match).
- 2026-09-09 (later) — "But the version is not bumped." Correct, and the reason is worth
  writing down: the number the UI shows is the CORE's, read from `/api/health`. Two releases of
  new sidebar behaviour went out bumping only `web/package.json`, which nothing in the running
  app displays — so from inside Jarvis the version sat at 2.0.0a30 across both, with no way to
  tell whether the new UI had actually landed. Bumping a version nobody can see is not a
  release marker.
  Fixed at both ends. The SPA's own version is baked into the bundle at build time (a `define`
  in vite.config.ts reading package.json) and shown in two places: under the core's number in
  the header, and as its own `web` stat next to `core` on the Status screen. Being compiled in
  rather than fetched is the point — it says which bundle the browser actually loaded, not what
  the server has on disk, so a stale cache is visible instead of invisible. And the core is
  bumped for this release too (2.0.0a31): the image it ships includes the SPA, so the number the
  app shows should move whenever what is deployed moves.
  Also: `web/package-lock.json` had been stuck at 2.0.0-alpha.1 for eleven releases. Synced.
- 2026-09-09 (later still) — The same filter bar on every screen that holds a list. One
  `ScreenFilter` (search box + toggle chips + an "n of N" count) and one `matchesQuery` — every
  word of the query has to appear somewhere in the row's fields, in any order, case-insensitive —
  now serve Schedules, Meetings, Skills and Boards. Client-side, deliberately: unlike the
  sidebar's search, these lists are already fully loaded, so there is nothing to ask the core for.
  Schedules also gets the **Active only** toggle Arsen asked for, with the enabled count on the
  chip, and searches the prompt as well as the name (a schedule is named once and then remembered
  by what it does — the prompt is shown under the name only when the prompt is what matched).
  Skills search names, descriptions and triggers; Meetings titles, hosts and status. Boards are
  the odd one: the notes live in the columns, one request each, so only a column can say how many
  of its notes match. Each reports its count up, the parent drops the columns holding nothing and
  can say "nothing matches" when none of them do, and a board found by its own NAME keeps all of
  its notes rather than being filtered down to none.
  Three things that only showed up once it was on screen. The highlight was `color: var(--fg)`,
  which on a pastel sticky note (always dark ink, whatever the theme) rendered the matched word in
  near-white on pink — invisible; a `--mark` token and `color: inherit` fixed it, and the wash is
  translucent amber so it works on any surface the theme does not own. Ctrl/⌘+K was hard-wired to
  jump to Chat, so pressing it on Schedules took you off the very screen whose search you wanted —
  it now focuses the filter of the screen you are on, and Knowledge's own search box answers the
  same event so the shortcut is uniform. And the mock core had never seeded a meeting, so the
  Meetings screen could not be worked on at all without a host to record from; it has two finished
  ones now.
- 2026-09-09 (night) — Why the DevBG weekly scan kept dying, and the two fixes it earned. The
  scheduled 07:05 fire failed on a one-off vLLM disconnect (`RemoteProtocolError`, 9 steps into
  90; two failed runs out of 138 in the whole database, so transient). But the two runs after it
  were **killed by our own progress guard while working correctly**, and that is the real story.
  `args_hash` was tool name + arguments, and `browser.read {"mode": "text"}` carries nothing that
  varies — the page it reads is chosen by the `browser.open` before it. So a correct
  open/read/open/read walk across five different Facebook searches registered as one call made
  five times, armed the guard at `repeated_call_threshold` (3), and after two nudges `max_nudges`
  promoted the third verdict to stop. Both runs died at the fifth read, two and a half minutes
  in, with five completed searches thrown away.
  The judge had no chance of catching the mistake either, because what it was shown was
  `summary[:120]` and the first ~100 characters of every one of those results were
  `[tab 1271060609] (20+) DevBG | Facebook — .../search/?q=`, with only percent-encoded Cyrillic
  past the cut. It invented a story that fit — "repeatedly reading the same tab ID … not
  correctly targeting the newly opened pages" — which is exactly backwards: `background.js` says
  "THE Jarvis work tab. One tab, reused across every `open`", so the same tab id is right.
  So: a repeat now means the same arguments **and** the same answer (`StepRecord.result_hash`),
  which is what "the work did not move" actually looks like, and a genuine loop still trips it —
  three identical failures of `outlook.send` included. And `render_steps` replaces the blind
  truncation: the shared head is stated once, each line then carries what is its own,
  percent-encoding is decoded (nothing can be reasoned about `%D1%81%D1%8A`), the result's size
  is shown, and the arguments are printed instead of a hash of them, since the difference between
  two steps is often exactly there. The judge prompt also says outright that identical arguments
  are normal for a stateful tool and to judge by the results. `step_of()` now assembles a step in
  one place instead of three hand-built copies in loop.py.
  Still open from that transcript, not fixed here: no retry on a dropped model stream, and
  `browser.read` will happily read a work tab another task navigated (one read came back as
  GitHub's traffic page for homelab-monitor).

- 2026-09-10 — Two more kinds of chat: incognito, and disappearing. Arsen's ask, in his words:
  a chat "where i can with great confidence share super personal stuff without the fear that
  this will be stored somewhere and will be considered in the chat in general", and a chat for
  the small tasks he then has to delete from the navigation pane by hand — "after X time
  inactive - it deletes itself".
  First the audit: what does an ordinary chat actually leak into the rest of Jarvis? Five
  things. The knowledge learner reads every finished exchange and writes entities and mentions
  into the graph (gated only by `kg_learning`). The titler sends the first exchange to the
  classifier and stores what comes back. `Store.search()` walks every title and every message.
  The sidebar row quotes the last message as a preview. And the model itself can be asked to
  pin a note or `kg.remember` a fact. A sixth, found along the way: attachment ROWS cascade when
  a chat is deleted, the FILES in `data/attachments` did not — every deleted chat left its photos
  behind. Fixed for every delete, not only these.
  Incognito is a flag on the conversation and a code-level no at each of those five doors: the
  learner is skipped, the titler is skipped (the title is the constant "Incognito chat" until
  Arsen renames it), search adds `incognito = 0` to both queries, `touch_conversation` keeps
  the preview NULL, and the `notes.*` / `kg.*` writers are filtered out of the tool list the
  model is offered — and refused with a reason if it names one anyway. The stable system prefix
  gets one short "Private conversation" block so the model does not promise what it cannot do.
  Nothing else is taken away: schedules, mail, the web, the boards for READING all still work —
  a private chat is not a crippled one. What the prompt cannot enforce the code does; what the
  code cannot know (that Arsen explicitly wants something saved) stays possible in a normal chat.
  Decided against: turning an existing chat incognito. What it already taught the graph cannot
  be un-learned, so the flag is set at birth or not at all — the SPA says so in the menu.
  Disappearing is `ttl_seconds` plus a derived `expires_at` the store re-arms on every message,
  and a `Reaper` in the core that asks once a minute for what is due and removes it the way
  Delete does (runs cancelled, files unlinked, row gone, `conversation.deleted` to every client).
  A chat with a run still working is left for the next round: its reply re-arms the timer anyway.
  Three idle times only — an hour, a day, a week — refused rather than rounded, because a ttl is
  a promise about when something is gone. An incognito chat always has one (an hour by default)
  and can never be given "keep": that is what makes it vanish without Arsen doing anything.
  Decided against: "gone when you close it", the browser's incognito model. There is no window
  here — the same chat is open on the phone and the laptop, the PWA is suspended and resumed,
  a tab crashes — so "closed" is not a thing the core can see. A server-side idle timer is.
  In the SPA the choice lives in one chip next to the thinking chip in the composer: on a fresh
  chat it picks what the first message will open (kept / incognito / gone after 1 h, 1 d, 1 w)
  and travels with that `run.create`; on an existing chat it shows the state — "Incognito · 58
  min left", "Disappears in 23 h" — and changes the timer. The row menu and the top-bar menu get
  "Disappear after…" as a second-level picker like "Move to folder…". An incognito chat is said
  out loud on the chat itself, in a banner above the transcript, because the chip is small and
  this is the one thing to be sure of before typing; its sidebar row shows a crossed-out eye and
  "Incognito · gone in 58 min" instead of a preview, and never a quote of what was said - the
  client-side preview fallback (newest loaded message) is bypassed for it too, as is the local
  title narrowing in search.
  Said honestly in API.md: while it lives, an incognito chat IS rows in SQLite on ardi — the
  engine resumes runs from the database, there is no way round that. The promise is about what
  leaves the chat (nothing) and how long it stays (its ttl), not about the disk under it.
  (core 2.0.0a34, web alpha.14; migration 0009; 13 new core tests, 3 new web tests.)
- 2026-09-10 (later) — Incognito no longer implies disappearing. Arsen, ten minutes after the
  deploy: "why incognito is also disappearing? I am selecting only incognito and it still
  disappears". My coupling, and the wrong one. The reasoning had been that the transcript is rows
  in SQLite while the chat lives, so a chat that promised to keep nothing should not sit there
  forever - but that conflates two questions Arsen wants answered separately: what is remembered
  (incognito) and how long the chat stays (the timer). Now they are independent. An incognito
  chat has no timer unless given one, "Keep this chat" is on its menu like any other, the draft
  chip is a toggle plus a timer so the two can be combined, and the banner says "It stays until
  you delete it" or names the timer. `INCOGNITO_DEFAULT_TTL` is gone from proto and SPA; the 422
  for keeping an incognito chat is gone from REST and the mock; the migration's comment is
  corrected (comment only - the schema it applied is unchanged). (core 2.0.0a35, web alpha.15)
- 2026-09-10 (evening) — First real incognito session, and three lessons. Jarvis reminded Arsen
  of a dinner reservation he had made in another chat; asked how it knew, it said it must have
  hallucinated. It had not: the reservation is a note on his boards and an entity in the graph,
  both injected into every prompt, incognito included - by design, and Arsen confirmed the
  design ("инкогнито ТРЯБВА да има всички знания. Но се притеснявам да не пише нещо някъде").
  So the private-conversation block now says the reads are open and only what is NEW goes
  nowhere, and tells the model never to claim a guess for something it read off the boards.
  The lag he felt was not the release: one chat spent 8 minutes generating 16,384 tokens of
  reasoning until the cap (finish=length; the guard then turned thinking off and it answered in
  14 s), while a continued Burnout Prevention run carried 47-75k-token prompts and fired 35
  calendar_create calls in one step - the two of them held the endpoint's concurrent slots, and
  the incognito chat's pre-flight waited 2.5 minutes for one. Zero warnings in the core log.
  And the dropdown was the wrong control: "искам иконките ... да не са дроп довн мену под чата
  а отделни иконки някъде, за да мога по-лесно и с един клик/тап да ги отбелязвам. доста често
  ще ги ползвам." Two toggles now sit where the chip was - the eye (incognito, before the first
  message) and the hourglass (a day of quiet, on a draft or a live chat; the ⋯ menus still offer
  an hour or a week) - pressed means tinted, and a small label appears only while on. The
  sidebar head gets a "New incognito chat" button next to "+", one tap from anywhere.
  (core 2.0.0a36, web alpha.16)
- 2026-09-11 — "От 2 дни много започна да се разфокусира." The AI Masterclass chat, read end to
  end. Turn one found a mail after thirty searches - triage had filed it into Action Hub/
  Reference and outlook_search looks only in the inbox - and dumped a 41k-character folder tree
  and a 13k-character account list on the way. Turn two asked for a business card "for the
  event" and the model had never heard of the event: the planner planned from the new sentence
  alone, it listed 21 days of calendar, found a Qorus webinar and chased that. Turn three hunted
  the mail from scratch again, PowerShell COM hacks included.
  The amnesia was ours. `trim()` counted every tool result at its stored size, while the model
  would only ever have been shown a 700-character head of an old one - so a turn with 110k
  characters of tool output against a 76.8k budget was thrown away whole, answer included. The
  compaction that should have bridged it summarised only the question, because `_cut_index`
  accepted the injected context message as a boundary. And every earlier turn's 6-10k of skill
  text rode along in full: four turns, 32k characters of repeated instructions.
  Now `earlier_turn_view()` runs before compaction and trimming: earlier turns' tool results are
  heads (same marker, same `jarvis.result_read` ref), earlier turns' context blocks are one-line
  stubs naming their skills, the current run is untouched. The head logic lives in context.py
  and the in-run compressor reuses it. `_cut_index` cuts only at a message Arsen wrote. Five
  tests, the Masterclass shape as the fixture: a 41k dump one turn back, the answer survives.
  Not a regression of a release: the trim rule is from 2026-09-05. What made it bite in the last
  three days is the environment - 82 tools per call since 09-08 (was 56), triage filing 58-124
  mails a day out of the inbox since 09-06, and the host's folder/account dumps. Still open,
  host-side: outlook_search across all folders by default, names-only folder and account lists;
  core-side: the planner seeing the previous reply. (core 2.0.0a37)
- 2026-09-12 — "Сложих Мими в алоуд лист. Изпрати мейла." The host said `sent: true`, the chat
  said "Изпратено ✅", and the Gmail Sent Mail folder stayed empty. The mail was in the
  corporate store: a draft under AApostolov@postbank.bg and a "sent" copy in its Outbox with the
  Exchange sender and `SendUsingAccount = None`. pywin32's `mail.SendUsingAccount = acct` is a
  by-value PROPERTYPUT that Outlook accepts and ignores for this object property; it wants
  PROPERTYPUTREF on dispid 64209. No exception, no warning, so nothing told anyone the mail
  was leaving through the wrong account. Now the host invokes the PUTREF form, reads the
  property back and refuses to send when it does not match - never a silent fall-back to the
  default account - and the reply carries `sent_via`. Recipients are resolved before a draft
  is saved (an unresolved draft grew a second copy of the address when Arsen opened it). And
  the mail can carry files: `attachments` = paths under fs.roots, fenced like fs_read.
  The fake COM ignores the plain assignment the way Outlook does, so the test proves the form
  that works. Verified against the real Outlook: Gmail → Gmail with a file, `sent_via` gmail,
  delivered.
  Found on the way, not ours: Outlook had been half-closed since ~19:20 behind two "empty
  Deleted Items on exit?" prompts, so no account's Outbox moved at all; and an Outlook started
  headless by COM un-submits Outbox items touched after a restart. (host 2.0.0a11)
- 2026-09-13 — Three things about a disappearing chat, from Arsen reading the screen. The timer
  toggle gave one idle time (a day) and a second tap took it away; he wanted the tap to walk the
  choices instead: off → 1 hour → 1 day → 1 week → 1 hour → …, one per tap, the default now the
  shortest one. `nextTtl()` owns that walk and the ⋯ menu keeps "Keep this chat", which is now
  the only way back to off. The hourglass became a dotted chat bubble — a chat that is not
  staying — everywhere the disappearing state is drawn.
  And the marking was not a marking: a 12px glyph in the same muted grey as the pin, no words,
  while an incognito row gets its own class and says "Incognito" in the preview line. "the
  disappearing sessions are not marked in the list with chats." Now the row carries
  `conv-disappearing`, the glyph takes the accent the composer's timer toggle already uses, and
  the preview line leads with "gone in 58 min" — ticking, because only a row that is counting
  down subscribes to the clock. The sidebar head gets a third starter next to "+": one tap for a
  new chat that is gone after an hour of quiet.
  Settings had grown past one screenful, so it gets a table of contents: a sticky rail of
  section chips (click to jump), a search box that narrows them by section name or by the field
  words each section holds ("timeout" → Model roles), Enter jumping to the best match, and
  Ctrl/⌘+K focusing it like every other screen's filter bar. The rail wraps to two rows on a
  desktop and scrolls as one row on a phone, and it measures its own height into
  `--settings-nav-h` so a jumped-to heading never lands underneath it.
  Two things the screenshots caught that the code looked fine for: an IntersectionObserver hands
  you only what CHANGED, so reading one callback's entries left the rail with nothing marked
  after a jump; and every "is it at the top of the page" band is really reading the strip hidden
  behind the sticky rail. The spy now measures against the rail's own bottom edge on scroll.
  (web alpha.17)
- 2026-09-13 (later) — Four from the phone, and one the model could already do.
  "on mobile when i write a new message the footer menu overlaps the chat inbox." The textarea
  capped itself at 40% of `window.innerHeight` - which, with the keyboard up, is 40% of a
  screen two thirds of which is keyboard - and only re-measured on a keystroke. And the empty
  "Talk to Jarvis" block had no `min-height: 0`, so on a new chat it kept its full height and
  shoved the composer under the nav. Now the box measures the room the chat column actually
  has (minus its own chrome, minus a floor for the transcript), listens to `visualViewport`
  resize, and the CSS fallback is `40dvh`. Playwright at 400×360 with a 14-line message:
  overlap 75 px before, 0 after.
  "take photo - не мога да променям резолюцията, винаги е като широка лупа. не мога да
  фокусирам ... моделът много често казва - снимката е много размазана." Three things, and
  the first was policy: on a phone with https the in-app getUserMedia preview won, and it is
  one fixed wide-angle frame with no tap-to-focus and no zoom. A phone now ALWAYS hands the
  job to its camera app (`capture=environment`); the in-app preview stays for a desktop, where
  `capture` is ignored. The other two were on the way in: re-encoding dropped EXIF, so a
  portrait photo reached the model lying on its side; and Pillow's default resample from 4000
  to 1568 px is soft, which reads as out-of-focus in an answer. `exif_transpose` first (even
  under the size limit), LANCZOS with `reducing_gap=3.0` after.
  "qwen3 27b поддържа и видео." Checked, not assumed: the served checkpoint's config carries
  `video_token_id` and a `temporal_patch_size`, and a test-pattern mp4 as a base64 `video_url`
  part came back described correctly from vader, no server flags changed. So: a `video`
  attachment kind. A clip is re-sampled ONCE on the way in (PyAV) - one frame a second, 32 at
  most, spread across the whole length so five minutes is 32 frames ten seconds apart - and
  re-encoded MJPEG-in-MP4 at 768 px, because every decoder reads that and `libx264` is exactly
  what was missing on ardi's ffmpeg. The message says what was shown ("32 frames over 90.0s").
  A poster frame in the transcript, playable in the lightbox, "Record a video" in the "+" menu
  on a phone. 90 s of 720p: 8.8 MB in, 356 kB out.
  "in the attachment button i see only Take a photo and Photo or file." Now four: Take a
  photo · Record a video (phone) · Photo or video · Document or file.
  (core 2.0.0a38, proto 2.0.0a17, web alpha.18)
- 2026-09-13 (evening) — "Е не успя ли да чуеш какво ти казвам във видеото." No: the frames
  carry no sound, and a vision model cannot read lips. The video pipeline dropped the audio
  track on the way in and the model, honestly, said so. Now the clip's audio is pulled out
  with PyAV as 16 kHz mono WAV - from the ORIGINAL bytes, the sampled clip has no audio -
  and goes through the same WhisperX the microphone uses; the words land as the attachment's
  `text`, and the message reads "[video attached … shown as 5 frames over 5.0s; what is said
  in it (bg):]" followed by the transcript. A clip with no audio track says "it has no audio
  track"; a Whisper that is down says "its sound was NOT heard" - the frames still go, but
  the model is never left to pretend. (core 2.0.0a39)
- 2026-09-13 (night) — "докато генерира отговор ... не ми дава да скролна до началото на
  отговора - винаги ме връща." Unpinning was decided by distance: a scroll that ended within
  48 px of the bottom stayed pinned. While an answer streams the transcript grows every line
  and the pin yanks it back down, and a finger on a phone moves a few pixels per event - so
  every swipe up ended "within 48 px" and was undone before the next one. Now DIRECTION
  decides: any user-driven scroll up unpins on the spot, and while a finger is down (or a
  gesture is fresh) the pin stays off entirely; scrolling back to the bottom re-pins. The
  synthetic repro only showed it at a realistic token rate (a line every 350 ms; at 30 ms the
  content outgrew the finger and the old code passed): swipe 200 px, kept 34 before, 123 after.
  (web alpha.19)
- 2026-09-13 (late) — "искам да мога да говоря с Джарвис ... слушалка. Аз говоря - Джарвис
  отговаря ... не пачвърк!" Story 10, written first, built in the four slices it names.
  The core owes a call five things and got them where incognito got its own: `Channel` on the
  run, the steer and the message; a voice run offered only `settings.voice.namespaces` (notes,
  kg, jarvis - on a call he thinks and remembers, he does not act), a tool outside them refused
  by name before the argument check, no planner, no thinking unless asked, and the "you are on
  a call" block as a context provider next to skills - per turn, never the cached prefix.
  The web got a `voice/` module with no React in it: a segmenter with a floor that warms up to
  the room, a sentence splitter that knows 3.5 and т.н. and initials, a listener that keeps raw
  16 kHz samples in a ring and writes one WAV per utterance (MediaRecorder's WebM carries its
  header in the first chunk only - a blob of mid-stream chunks is a file no decoder opens), a
  speaker that feeds speechSynthesis one sentence at a time and picks the voice by script, and
  the session: listen, transcribe, think, speak, listen, with cut-in (he stops mid-sentence, the
  words steer the run still working or start a new turn, the half-sentence written before the
  cut is dropped) and one honest spoken sentence for every failure. Twenty-two tests over
  fakes.
  The screen: the headset beside the microphone (the microphone dictates a message, the
  headset holds a call), a full-screen surface that is a pure function of the session's state,
  and on a touch device a lock that takes the light down and swallows every touch except a
  1.2 s hold - a web page cannot read the proximity sensor, so this is what "at my ear the phone
  does not press its own buttons" is made of, with a wake lock keeping the page alive. Both
  turns land in the transcript with a headset mark. Settings gets a Voice section (the tools a
  call may use, as ticks from the live tool list; think; the style block).
  Walked end to end in Playwright with an oscillator for a microphone and a stub for a voice,
  against the real core with the fake model (scripts/dev_fake_core.py): tap → locked →
  "Listening" → an utterance recognised → "Thinking…" → three sentences spoken in the Bulgarian
  voice → a tap on the guard does nothing, the hold unlocks → hang up → two spoken turns in the
  chat. Not yet done: the phone in the hand with a real headset - that is the sign-off the
  story asks for before the button counts as shipped. (core 2.0.0a40, proto 2.0.0a18, web
  alpha.20)
- 2026-09-14 — Three from the first calls. On Android "тегаво превключва между спийкър и
  слушалка"; on the laptop "стт слуша и отговорът на Джарвис"; and the voices: "на андройд
  звучи супер зле, на уиндовс е като робот". One root under the first two: while he speaks, the
  microphone hears him, and `speechSynthesis` plays outside the browser's audio path, so the
  echo canceller has nothing to subtract. Three answers.
  A route, since the web cannot pick the earpiece by name but CAN hold or release the
  microphone - which on Android is what puts Chrome in the phone's call mode (earpiece) or out
  of it (speaker). Earpiece is the default and holds the mic throughout; speaker lets it go
  while he speaks and takes it back when he stops - no echo possible, no cut-in either. A
  button, on the locked screen a hold, remembered per device.
  A gate that learns the echo instead of guessing at it: while he speaks it tracks the loudest
  the microphone has been (a slowly decaying peak, learned from what is NOT a cut-in), and only
  a voice 2.5× over that, held 450 ms, counts. Six tests with a synthetic TTS envelope: the
  echo never leaks, syllables and all; a voice over it does; the bar does not chase him up.
  And his voice: the core synthesises each sentence with Microsoft's neural voices (edge-tts -
  Borislav or Kalina for Bulgarian, Ryan for English; ~500 ms to first audio, cached on disk by
  sentence) and the page plays the bytes through WebAudio, fetching the next sentence while the
  current one plays. That last part is the real fix for the first two problems: audio the page
  plays is audio the echo canceller knows about, and it stays on the call's route. The device
  voice is the fallback when the core cannot, and a Settings choice. Said plainly in the
  setting: the text of each sentence goes to Microsoft. (core 2.0.0a41, proto 2.0.0a19, web
  alpha.21)
- 2026-09-13 — The send arrow Jarvis could not press. Testing DSK Bank's Webim chatbot,
  `browser.click @e9` on the send icon answered "the page did not answer … may be mid-navigation
  or a page extensions cannot touch" three times, and the model concluded the widget lived in an
  iframe it could not reach. It did not. The icon is an `<svg>`, the kernel ended its click with
  `(hit || el).click()`, and SVGElement has no `click()` - the TypeError left the kernel, Chrome
  handed the worker `result: undefined`, and the worker's only word for that was the iframe one.
  Reproduced in the jsdom harness in one file, fixed in two layers: an `activate()` that calls
  `click()` where it exists and otherwise dispatches a bubbling click at the element's centre -
  what a mouse does, handlers and activation included; and a try/catch around the whole
  dispatch, so a kernel exception comes back as `ok:false, kernel_error:true, "The page script
  failed while doing click: …, this is a Jarvis bug, not the page"`. Two tests pin both. The
  extension is loaded unpacked, so the fix lands on the next Reload at brave://extensions.
  Same afternoon, the second half of that transcript: with click gone, the model sent with
  Enter and then waited for the bank's bot the only way it could - `workocholic.shell_run
  Start-Sleep 45` followed by `browser.read`, five times, each read identical, the supervisor
  nudging it twice for "a loop" while the bot took two and a half minutes to answer - and then
  re-sent the question it had already sent. It had no clock. `browser.wait {text?, timeout_s?}`
  is that clock: it polls the page text (the frame it last read) once a second until it changes
  and holds still for 1.5 s, or the phrase appears, and returns only what is new - the reply,
  not the page again; on timeout it says "nothing changed, do not re-send". Read-only, the one
  call allowed to take its time (the worker's 30 s call budget yields to its timeout_s, and the
  core already honours a tool's own `timeout_s`). Nine tools now; three transport tests.
  And the side panel itself: "през екстеншъна не мога да отворя чата по темата". Panel mode
  had stripped the header, the sidebar AND the drawer, so the panel was one conversation with
  no way to another. It is phone width, so it gets the phone's answer: the mobile header (☰ →
  the conversation drawer, the title, the chat menu, the connection dot; bell and theme
  buttons left to the full app) and the drawer itself. Picking a chat routes to
  `/c/<id>?mode=panel` inside the iframe. Walked in Playwright at 380px. (web alpha.22)
  One more from the phone: "в voice chat текущото изречение е подчертано, но с тъмни букви на
  тъмен фон". The locked call screen paints its ground #000 whatever the theme, but its ink
  came from the theme's tokens - on the light theme the current sentence was #141a2b on black.
  `.call-locked` now carries the dark theme's ink and surface tokens itself. Verified: on the
  light theme the sentence being spoken renders rgb(234,240,251) on rgb(0,0,0).
- 2026-09-13 — "I like it and I don't like it. How to make it AI agentic and not patchwork."
  Three patches in one morning - the svg click, the wait, the hotspot cap - each a lesson the run
  had already learned by its end, hand-carried into the extension by me. So the afternoon went
  into the shape rather than the next patch. Four moves. Tools return EVIDENCE, not verdicts: a
  click that did nothing now says what was dispatched, the element's path, box, cursor and state
  (`evidence(el)` in the kernel); "no result came back" says what that means and nothing more;
  the outline names icon controls by class (`svg.webim-ico-send 27x27`) and ranks hotspots
  (beside a field, icon-sized, in view, a telling class) instead of taking the first 25 in
  document order. `browser.eval` - the model's own JavaScript in the work tab, `$`, `$$`,
  `$ref('e9')`, `describe`, `evidence` at hand - so the next mis-scored control is the model's to
  solve, not mine. The model SEES: `handle_result` had been dropping browser.screenshot's image
  since day one while the text told the model "attached for a vision model" - blind and believing
  it had eyes, on a multimodal endpoint. The image is now an attachment on the tool message and
  reaches vLLM as an image part in a user turn right after it; the tool card shows it. And the
  lesson goes to Jarvis, not to me: `skills.learn` writes a playbook (frontmatter `sites:`), a run
  that fought a site and then got through gets one written for it by the reflector, and the next
  browser result on that host carries the playbook before the first click. Two smaller ones that
  explain this morning: the browser context block says when the tool set changed (this
  conversation kept sleeping through shell_run because nobody told it browser.wait exists), and
  the supervisor's nudge is handed the family's unused tools and told to name the door, not the
  loop. Ten browser tools; 84 extension tests, 338 core, 92 web. (core 2.0.0a42, proto 2.0.0a20, web alpha.23)
  Evening, same chat: "виж пак сесията, пак не може да кликва". It could not, because the
  extension in Brave was three fixes old - the core had been redeployed twice, the extension
  reconnected each time (the log said "9 tools", the tell), and nobody had pressed Reload. What
  ran was the unranked outline, the send arrow past the length cap, and a ref the model made up
  from the part it never read. Two closures. `browser.find` now matches icon-only controls by
  class token ("send" → svg.webim-ico-send), and the outline drops sub-10px decorations when real
  icon controls exist. And the manual step is gone: the worker handles `browser.reload`
  (`chrome.runtime.reload()` re-reads an unpacked extension from disk), the core exposes
  `POST /api/browser/reload`, and deploy_ardi.sh sends it after health. One last Reload by hand
  to get the worker that knows the message; manifest 2.1.0 so the hello line shows which one is
  loaded.
- 2026-09-13 — A wait tool for the whole of Jarvis, not just the browser. "since it is GPAI …
  can keep a session alive as long as needed with this wait and after resumes." Until now the
  only wait was browser.wait; for anything else the model reached for
  `workocholic.shell_run Start-Sleep`, host-specific, counted against the 120 s tool budget, and
  read as a loop. `jarvis.wait {seconds, reason?}` is the general one: sleep server-side up to an
  hour a call (chain for longer), cancellable at once by a Stop, and — the part that makes it
  usable — the run clock is PAUSED while it sleeps (`RunWatch.paused_s`, subtracted from
  `elapsed_s`), so waiting for a fifteen-minute build no longer trips the ten-minute time budget,
  and the per-call deadline is the wait's own duration, not `tool_timeout_max_s`. browser.wait is
  credited the same way. Six tests. It is not a schedule: a schedule fires a fresh run hours or
  days later; wait holds THIS run open across minutes so the task resumes with all its context.
  (core 2.0.0a43)
- 2026-09-13 (code red) — "incognito - videos and media uploaded are stored to the device!!!!
  INCOGNITO MEANS INCOGNITO!!!!!" True, and by design until tonight: API.md said an incognito
  chat's attachments sat in `data/attachments` "while alive". On the core there was one - the
  clip he had recorded at 20:35 into an incognito chat, the file on the volume, the transcript
  in the row; on the phone the same clip was in the gallery, because "Record a video" is
  `<input capture>`, and the OS camera app keeps what it records. Four leaks closed, one rule.
  The core: an incognito attachment never touches the disk. Bytes, the text read out of it, the
  thumbnail - all in the process's memory (`_held`), the row with `path` and `text` NULL; a
  restart forgets them and the transcript says so (410). The upload that comes BEFORE the chat
  exists carries `incognito: true` (the core had no way to know), the bind pulls a file an old
  client wrote off the disk before the message is published, and start-up scrubs whatever an
  incognito chat still has on the disk - which removed tonight's clip. Every response for one
  is `Cache-Control: no-store`: the phone's browser had been told to keep the thumbnail and the
  clip for a day. The voice cache too: `/api/tts {cache: false}` on an incognito call.
  The phone: in an incognito chat the OS camera app is never used. Photo through the in-app
  stream (the blurrier wide lens is the price), clip through a new in-app recorder
  (getUserMedia + MediaRecorder, chunks in memory, 120 s cap, "nothing is saved to this
  phone" on the screen), and where there is no in-app stream (plain http) there is no camera,
  and the menu says why. And one more found on the way: a call started with the eye on was
  opening an ordinary, remembered chat - `run.create` from the call never carried the draft's
  privacy. Now it does. Six core tests, five web.
  Same hour: "the tts in the chat call is not working! so today you broke plenty of things".
  The core was fine - `/api/tts` 200, a valid 24 kHz MP3 - so the page was not playing it. The
  ServerSpeaker opened its own AudioContext lazily, from a WebSocket delta, with the phone
  already in call mode (microphone held = earpiece); a context that comes up suspended never
  ends a source, and the call sits on "Speaking" in silence with the screen saying nothing.
  Cannot be proven from here without the phone, so the fix does not depend on the diagnosis:
  the voice now plays through the microphone's own context (opened in the "start call" tap,
  proven running by the level ring); `resume()` is awaited with a 1.5 s deadline and a context
  that will not run hands the sentence to the device voice; every play has a watchdog a little
  longer than the clip; and whatever went wrong reaches the screen through
  `Speaker.takeProblem` - "The browser would not play the voice (audio suspended); using the
  device voice." - instead of passing in silence. If it was the earpiece all along: it is the
  default he asked for this morning, the sound is at the ear, and the button (a hold on the
  locked screen) is the speaker. (core 2.0.0a44, web alpha.24)
- 2026-09-15 — "the AI said the videos are rotated, and sees only 30 seconds, not the full
  video." Both true, and a third one found on the way. The 30 seconds: `_prepare_video` took
  32 frames evenly across the clip and muxed them at `VIDEO_FPS` = 1 fps, so the FILE was 32
  seconds long whatever the clip was. vLLM's loader (checked in the container on vader:
  `opencv.py`, `duration = total_frames_num / original_fps`) hands Qwen3-VL that clock, and the
  model timestamps every frame from it - the frames spanned the two minutes, the timestamps
  said 0-31 s, and the note said 119.97 s, so the model concluded, reasonably, "видеото е 2
  минути, а аз стигам само до първите ~30 секунди" (this morning's blood-pressure clip,
  `att_1a0a3946f8c`). The file is now muxed at the sampling rate - 32 frames of a two-minute
  clip at 4/15 fps - so it is as long as the clip; verified through vLLM's own
  `VIDEO_LOADER_REGISTRY.load("qwen3_vl").load_bytes` in the container: 32 frames,
  `duration 120.0`. The note also says "one every 3.7s" now. The rotation: a phone held
  upright writes landscape pixels and a display matrix; `frame.to_image()` is the raw pixels.
  PyAV 18 exposes `frame.rotation` from the matrix, and `image.rotate(frame.rotation,
  expand=True)` reproduces ffmpeg's autorotate pixel-for-pixel (checked against `ffmpeg -i
  rot.mp4 -frames:v 1` on `-display_rotation ±90` files, mean diff 0.0) - applied to the sampled
  frames and to the transcript poster. The third: a WebM out of the browser's MediaRecorder -
  the in-app (incognito) recorder on Android - carries NO duration anywhere, the container
  said 0 s, and 0 s at one frame a second is ONE frame of a two-minute recording. The
  packets are demuxed (no decode) and the last timestamp is the length. And a fourth, small:
  the sampler re-anchored each pick on the frame it actually got (`next_at = at + step`), so
  the step drifted by up to a source frame per pick and a 40 s clip at 5 fps came out 29
  frames; picks are on a fixed grid now (`k * step`). Three tests: the 60 s clip's stored file
  is 60 s long, a `-90` display-matrix clip is stored and postered portrait, a streamed WebM
  is 32 frames over its whole length. (core 2.0.0a45)
  Same evening: "raise it to 64 frames". `VIDEO_MAX_FRAMES` 32 → 64: the two-minute clip is now
  one frame every 1.9 s instead of 3.75 s, for roughly twice the tokens (~10k of the 262k
  context per video). The file's clock follows: 64 frames of 120 s at 8/15 fps. (core 2.0.0a46)

## 2026-09-15 — Jarvis2 moved to the single-card vLLM on vader (:18021)

vader now runs two vLLMs. `vllm-q38` (TP=2, INT8, 262K ctx) was repinned to GPU0+GPU1 (the two
Turbo blowers); the syv-ai `qwen38-27b-single` (W4A16 AutoRound + DFlash2 MTP, 64K ctx, KV pool
68.6K tokens, max-num-seqs 8) sits alone on GPU2, the ROG — the coolest card. `VISION=1` was added
to its `.env` (tower offloaded to host RAM, KV pool unchanged) because the default
`--language-model-only` rejected every image with "At most 0 image(s)".

Settings changed through `PATCH /api/settings` (no deploy): every role's `base_url` →
`http://100.76.27.18:18021/v1`; `roles.chat.max_tokens` 32768 → 16384, because the worst case
prompt (~15.7K system+tools + 24K history budget + attachments) plus 32K output overshoots the
64K context and vLLM refuses such a request outright rather than trimming it.

Measured through the core itself (WS `run.create`, incognito, 15.7K-token prompts, deleted after):

| | TP2 :8010 | single :18021 |
|---|---|---|
| one chat, warm prefix | 39–43 tok/s, ~35–48 s | **186–199 tok/s, ~8 s** |
| 3 concurrent chats | 50 tok/s aggregate, wall 146 s | **300 tok/s aggregate, wall 22 s** |
| 4 concurrent | — | 294 aggregate, per-chat 118–196 |
| image (PNG, base64) | works | works, 3.3 s, correct |
| video (9 s mp4, 9 sampled frames) | — | 6.6 s, order right, invented an "orange" between red and green |

Cold prefill of the 15.7K prefix costs ~12–13 s on either server (the chat after a restart);
after that the prefix cache serves 15.2K of the 15.7K. MTP acceptance on Jarvis prose ≈ 70 %
(23,067 of 32,718 draft tokens), well above the 25–30 % of the synthetic bench — which is why
the core sees ~190 tok/s where the raw bench saw 125.

The third and fourth concurrent chat wait ~15 s before their first token: that is
`max_concurrent_runs_per_endpoint = 2` in the core, not the GPU (model TTFT stayed 0.6–1.0 s).
The reason that setting stops at 2 — long runs evicting each other's prefix out of a 305K pool —
does not transfer to the 68.6K pool one-to-one; the prefix is shared, so 3 runs cost one copy
plus their outputs. Left at 2 for now; raise to 3 if the wait shows up in real use.

Left open: W4A16 answer quality against INT8 (`bench/prefix_test.py` on vader against :18021).
Rollback = the same PATCH with `:8010` and `max_tokens 32768`; the TP2 server is still running.

### Same evening — first real use died: HTTP 400 "maximum context length" (core 2.0.0a47)

Two news searches in one run put 49,153 tokens in the prompt; with `max_tokens 16384` vLLM
answered "at least 65,537 tokens" and refused. The 262K server never showed this because Jarvis2
sends a fixed `max_tokens` and there was always room. The adapter now retries with a halved
`max_tokens` while the server keeps refusing (a 400 costs a round trip, not a prefill), stopping
at 256 — below that the prompt itself is the problem and the run fails with the server's message.
Trap found on the way: the 400's "your prompt contains at least N input tokens" is not the prompt
size, it is context − max_tokens + 1, so the exact fit cannot be read off it (the first version
of this fix trimmed 17 tokens and got refused again). Three tests. `roles.chat.max_tokens` sits at 8192 (a stopgap set before the fix) — with the clamp in
place it can go back up, and 16384 is the sensible value on a 64K server.

### Later — the single server moved to `CTX=long` (int8 KV, 131072 ctx, KV pool 136,429 tokens)

The 64K ceiling was the default `CTX=fast` profile, not the model. `CTX=long` in
`~/qwen38-27b/.env` on vader; `.env.bak-fast-20260915` is the way back. Measured (same harness,
cold cards, cold prefill), fast → long: short decode 125 → 114 tok/s; 2.8K prompt prefill
1196 → 1265, 11K 1173 → 1104, 33K 1073 → 805 tok/s (−25 %); decode at 33K 95 → 92; a 68K prompt
(impossible before) prefills at 562 tok/s (121 s cold) and decodes at 64. Four concurrent short
streams got *better*: 132 → 237 tok/s aggregate, TTFT 5.6 → 1.1 s. Through the core: chat decode
182–195 tok/s (unchanged), cold TTFT on the 15.8K prefix 12.7 → 15.7 s, warm 0.6 → 1.5 s.
`roles.chat.max_tokens` stays 16384; the halving retry stays as the safety net (95K scheduled run
+ 16K answer still overshoots 128K).

## 2026-09-16 — Jarvis2 on the syv-ai image with TP=2 (GPU0+GPU1, :18022); the think-toggle cache bug

Overnight a scheduled run and an agent chat ran together and the visible speed fell to 3.9 tok/s.
Two causes, both found by measurement:

1. **`adaptive_thinking` was defeating the prefix cache on every step.** The Qwen3.8 chat template
   prepends "Reasoning effort is set to xhigh. …" to the system prompt when `enable_thinking` is on
   and nothing when it is off. Alternating think on/off between steps therefore changed the first
   36 tokens of the prompt, and every step re-prefilled 33–47K tokens (`cached 0 (0%)` in the run
   inspector, TTFT 40–85 s). Confirmed with a raw test: same prompt, think toggled → `cached 0`;
   `reasoning_effort=medium` (no line) → hits. Fix on the server, not in the core: a patched copy of
   the template (`chat_template_stable_prefix.jinja`, line 46 `if true`) emits the line in both modes,
   mounted read-only and passed with `--chat-template`. Only `xhigh`/`medium`/`low` are valid effort
   values — the core's `ThinkLevel` "high" would 400 against this template; `think_level` stays None
   (= xhigh, what Arsen wants).
2. **The 136K pool could not hold two ~45K contexts plus a chat**; each step evicted the other run's
   prefix. Solved by the move below (357K pool).

The syv-ai image runs under TP=2 on the two Turbos (`~/qwen38-27b-tp2/`, `EXTRA_ARGS=--tensor-parallel-size 2`,
`MAX_LEN=262144`, `CTX=fast` bf16 KV, `VISION=1`): KV pool **357,833 tokens**, 262K context, and it
beat the single card everywhere — decode 135/137/121/112 tok/s at 0/4K/11K/33K (single `long`:
114/120/105/92), prefill flat at ~1.3–1.4K tok/s (single: 805 at 33K), 4 concurrent 264 vs 237.
Through the core with xhigh thinking: warm chat **204–236 tok/s**, TTFT 0.6 s, `cached 15232`;
3 concurrent chats 319 tok/s aggregate at ~200 each. All roles → `http://100.76.27.18:18022/v1`.
`vllm-q38` (stock vLLM, INT8) is stopped; the single-card server on GPU2 (:18021, `long`, same
template) stays up as the spare lane.

### Same morning — lanes and run routing: a run executes on ONE endpoint, chosen in settings (core 2.0.0a48, web alpha.25)

Even with the prefix cache hitting, a scheduled run whose steps are 84k tokens adds 13–27k new
tokens per step, and while vLLM prefills those in 2,048-token chunks the chat on the same engine
gets one decode step per chunk: 2–9 tok/s in the run inspector, 122 in between. Not a scheduler
knob to turn — a separation to make, and made as one rule rather than a patch:

- `chat` and `background` are the two **lanes**: full model specs, the same main loop, the only
  roles that think. `settings.run_routing` maps every run kind to a lane (default: chat, collab,
  system → chat; scheduled, triage, meeting → background) and is edited in Settings → Run routing.
- A run is routed **whole**. The engine sets a context variable with the run's kind for the run's
  task; `AdapterFactory.for_role` reads it, so the planner, classifier and judge calls a run makes
  keep their own behaviour (thinking off, temperature) but execute on the run's lane endpoint —
  no plumbing through eight features. Outside a run a role uses its own endpoint; the triage mail
  sweep, which is not a run, is routed like triage runs on purpose.
- Settings stored before the lane existed load `background` as a copy of `chat` (never the
  localhost default). Validators refuse a non-lane in `run_routing`. Six tests.

Layout on vader after this: chat lane → the single card on GPU2 (`:18021`, 128k, ~190 tok/s,
nothing else runs there); background lane → TP=2 on GPU0+GPU1 (`:18022`, 262k, 358k-token pool,
flat ~1.3k tok/s prefill — the box that holds 84k-step runs well). The alternative on the table —
three single-card engines behind a sticky router — was set aside: it gives up the 262k pool the
background runs actually use, caps every run at 128k, and adds routing logic with its own
failure modes. It stays the upgrade path if the chat lane ever gets crowded, and with lanes
in settings it would be one more lane, not a new mechanism.

Correction an hour later: the lanes were assigned the wrong way round. An 80k-token chat (system
16k + history 24k + tool context 40k — the budgets' sum) ran at 7–10 s TTFT and ~30 tok/s per
step on the single card, whose int8 path decays hard past ~25k. The strong engine belongs to
the run someone is watching: chat → TP=2 (`:18022`, flat prefill, ~110 tok/s at 80k),
background → the single card (`:18021`, 128k — enough, since the budgets bound a prompt at
~80–90k; unattended runs can afford the slower step). Two endpoint fields in Settings, no deploy.

## 2026-09-16 — the context economy: a run carries a view of its ledger, sized to its lane (core 2.0.0a49, web alpha.26)

An 81k-token chat step turned out to be exactly the sum of the budgets — system+tools 16k,
history 24k, this run's tool results 40k — with a 42,900-char `outlook_folders` listing, a
30,581-char `outlook_search` and a 20,223-char `shell_run` riding along whole because a result
entered the prompt at any size the step it arrived in. Prompt size is what makes steps slow at
depth, what made runs collide on one engine and what produced the 400 on the 64k server; the
lanes moved that load around, this changes how much is asked to be read. Three rules, one place:

- **Admission** (`engine/context.py: admit`, applied in `AgentLoop._tool_message` and on a
  resumed run's rebuild): a tool result longer than `tool_result_admit_chars` (12k, ~3.7k tokens)
  enters as its head plus the `[truncated …]` marker naming the ref — from the step it arrives
  in, for builtin, MCP and browser tools alike. A mail body or a search page passes whole; a
  folder dump does not. `tool_result_head` now takes the head size, so the admitted head and the
  700-char aged head are one function and the first can age into the second (the marker carries
  the full length). Nothing is summarised, nothing deleted: the DB keeps every byte.
- **Ceilings that follow the lane** (`Settings.effective_budgets`): the history and results
  budgets stay as configured unless the lane's window — `max_model_len` from `/v1/models`, asked
  once per endpoint by `AdapterFactory.context_window`, `num_ctx` overriding — cannot hold them
  next to the system message, the tool schemas, `max_tokens` and `context_reserve_tokens`; then
  both shrink in proportion, never below a 4k floor. On a 64k lane with 16k answers that is
  11.7k + 19.4k instead of 24k + 40k, and the 400 cannot happen by construction; the halving
  retry stays as the net.
- **Retrieval without reading it all**: `jarvis.result_search(ref, pattern)` — case-insensitive
  regex (plain text when the regex is bad) over a stored result, matching lines with context and
  char offsets, so the one folder in 43k chars is one call, not five 8k pages. Rule 6 says so.

And **visibility**: every `model.call` event carries a `ContextBreakdown` — sys / tools / hist /
res in estimated tokens, the lane window, how many results ride as heads — and the run inspector
prints it (`ctx 33k = sys 16k + tools 3k + hist 5k + res 9k / 262k · 2 results as head`).
Settings → Behaviour has the four knobs; `tool_result_admit_chars = 10_000_000` is the
no-deploy way back to the old behaviour. Nine new tests; 273 pass.

Two things the first live run taught, fixed before the day was out: (1) `result_search` was
line-based, and a 43k-char MCP result is ONE line of minified JSON — every "hit" was the whole
thing. It is window-based now (characters around each match, windows merged where they touch,
offsets kept), which works on JSON and prose alike. (2) 3.2 chars a token under-counts this
workload by ~40% (estimated 50k, served 79k: JSON punctuation and Cyrillic both tokenise
short), so the assembler now calibrates itself from every served step — `observe(prompt_chars,
prompt_tokens)`, an EMA clamped to 1.5-4.5 — and the breakdown, the budgets and the ageing all
use the learned ratio. One more note for tuning: ageing rewrites older results in place, which
invalidates the prefix cache once per compaction (a 46 s TTFT seen at 80k); on a 262k lane a
larger `tool_context_token_budget` buys fewer of those.

Third lesson, same day: at 12k the admission head made the model FISH. The folders question
took 60 search/read calls and six minutes on the new build against one step on the old one —
structured data has to be seen to be reasoned about; search helps once you know what you are
looking for. So `tool_result_admit_chars` is 48k by default (the head is for the genuinely huge:
page dumps, long shell output), and `Settings.admit_chars` caps it at half the step's results
budget so a small lane still admits proportionally. The fake adapter now reports prompt tokens
from the prompt it was sent (3.2 chars a token) so calibration in tests stays where the tests
were written.


## 2026-09-16 — the call: one voice per reply, one turn per breath, and no plan read aloud (core 2.0.0a50, web alpha.27)

Arsen: "voice call is super buggy — voices change, it cuts me off, sometimes the thinking tokens
get spoken", on the phone and the desktop alike. The transcript of this morning's call had all
three, each with a cause of its own:

- **Voices changed** because the language was decided per sentence (`scriptLanguage`, by
  design): a Latin-only sentence inside a Bulgarian answer got the English voice. Now the first
  sentence with letters decides the reply's language and the rest follows (`CallSession.langFor`);
  the next reply decides afresh. And the server voice, once it had to fall back to the device's
  for a sentence, stays there for the rest of that reply (`ServerSpeaker.beginReply`) instead of
  the two taking turns.
- **Cut off / answered twice** because a pause for breath closes an utterance (`releaseMs` 750):
  "да може в крайна сметка" and the rest of the sentence became two turns four seconds apart,
  both answered, spoken over each other. A recognised utterance now waits `joinMs` (600 ms) for
  him to go on and joins what follows; words recognised while the run is still being created are
  held and steered to it once it has an id — one turn, nothing lost, nothing doubled. Also:
  Whisper guessed the language of a short utterance and came back with Greek letters
  ("Διάλα, φορμουλήρε εγώ"); the call's language now goes along as a hint, and he can still switch
  to another of `stt_languages` when he is heard doing so.
- **"Thinking" spoken** was not thinking: with thinking off, a step that ends in tool calls writes
  its plan as plain text first ("The user is on a voice call. I need to keep it short. Let me…")
  — the tool format invites reasoning before a call — and every text delta was read out. A step's
  words are now held until the step ends and spoken only if it ended in an answer
  (`onStepDone`); replies on a call are two or three sentences, so the wait is a fraction of a
  second. Belt and braces: the voice style block says to say only what one would say aloud, and
  the always-present reasoning line in the patched chat template now ends "…when this turn has no
  thinking, answer directly and never narrate your reasoning" (loaded on `:18021`; `:18022` at
  its next restart).

Tests: joining, the run-id hold, the language hint, one voice per reply, the tool-call step. The
CSS he also called out is next, separately.

## 2026-09-16 — the wall: a step must fit the lane (core 2.0.0a51)

Checking for scheduled runs broken by context since the lane split found one: the AI
Newsletter draft at 05:30 read **31 mails in one step** — 222k chars of fresh results — and the
background lane (131,072) refused it: "requested 256 output tokens and your prompt contains at
least 130,817". Per-result admission bounds one result; nothing bounded a step with thirty. The
halving retry did its job and bottomed out at 256, which is the honest failure when the prompt
itself is the problem — so the prompt must never be the problem: before every model call, if
the messages exceed `window − max_tokens − reserve` (in chars, at the calibrated ratio, minus
the tool schemas), `_fit_to_window` cuts this step's fresh results largest first and only by
the overflow, never below the 700-char head, markers naming the refs. Ageing is the budget a run
lives within; this is the wall it cannot hit. On the 262k lane the same step fits whole.

An hour later (web alpha.28): "it catches every sound but not me, and comes back in Urdu". Two
things. The join window was being extended by the microphone hearing *anything* — in a room with
noise his recognised words were held until the room went quiet. Now only recognised words extend
the wait, and a hard deadline of 1.5 s from the first held words sends them regardless. And the
language hint could flip to "en" on Whisper's say-so alone (it labels a noise "en" as readily as
anything), after which Bulgarian speech came back as an English *translation*; the switch is
believed only when the words are written in that script too. The Urdu was the old bundle still
open in a tab — no hint at all, Whisper guessing on noise; a reload gets the new one (the
service worker keeps index.html network-first, hashed assets cache-first).


## 2026-09-17 — every limit a request can hit, closed at once (core 2.0.0a52, web alpha.29)

"HTTP 400 again on my last chat": `At most 1 image(s) may be provided in one prompt`. Not context
this time — the syv-ai image ships vLLM's default of one picture per prompt, and a chat with two
photos in its history is refused whole. Arsen's point stands: since the server changed, each of
its limits has surfaced one at a time, in his chats. So the request's limits are now an
enumerated list, each with a Jarvis-side bound that makes the request fit *before* it is sent,
whatever the server:

| limit | Jarvis2 bound |
|---|---|
| context length | budgets shrink to the lane's window (a49); the step is cut to fit (a51) |
| `max_tokens` | the halving retry (a47) |
| pictures/clips per prompt | `media_in_context` (4): the newest ride as pixels, older ones keep a note saying they were shown earlier; lanes raised to 6 images / 2 videos (`--limit-mm-per-prompt`, both `.env`s) |
| picture size | the server scales to 2,048 image tokens (`--mm-processor-kwargs`, shipped) |

Both lanes were recreated with the new args and the neutral reasoning line (`:18022` had not
had it yet). Test: two photos in a chat with the budget at 1 — the newest as pixels, the older
as a note. Rule 1 for the future: a new server is checked against this table before Jarvis
points at it, not after.

## 2026-09-17 — the newsletter run that took an hour and wrote nothing (core 2.0.0a53)

Arsen: "the chat is broken — this did not happen before", the 08:30 AI Newsletter. It ran for
an hour on the background lane, read 26 mails, then spent four to five minutes per step until
the time budget ended it, with `cached 0` on most steps. What happened, in order:

1. `tool_context_token_budget` was 40,000 under a 3.2-chars-a-token assumption, i.e. 128k chars
   of room; calibration made the token count honest (~2 chars a token on mail JSON), and the
   same 40,000 became 80k chars — the newsletter's results crossed it at mail twelve.
2. From then on ageing rewrote older results **every step** (compress to 70% is one step's worth
   of growth away from the next trigger), and every rewrite changes the middle of the prompt,
   so the prefix cache served nothing: 60–80k tokens re-read per step.
3. On the background lane that is 500–800 tok/s and a decode that crawls under it: four minutes
   a step. On the chat lane the same step is ~40 s. The routing put the heaviest reader on the
   slowest engine.

Three changes, one idea — a run's results should not be shuffled while it works, and the room
should come from the lane, not from a number tuned under a wrong constant:

- `tool_context_token_budget` 40,000 → **120,000**: the room in chars it always meant to be; the
  lane's window is what caps it (`effective_budgets`), so the 128k lane still gets ~77k tokens
  of results and the 262k lane the full 120k.
- Ageing, when it must happen, goes to **half** the budget (`_COMPRESS_TO` 0.5) and then not
  again for a long while — one cache miss, not one per step.
- `run_routing` default: **scheduled → chat lane** (262k, 1.3k tok/s prefill, 200 tok/s decode);
  background keeps triage and meeting, the runs that stay small. Settings → Run routing changes it.

Tests updated (routing expectations); 275 pass. The run is re-fired after deploy as the check.


## 2026-09-19 — the phone at the ear: black, still, and deaf to a cheek (web alpha.30)

Arsen, from a call on the earpiece: "I put the phone next to my ear — I need the screen to go
black and not be able to touch it, like a phone call; when I pull it off, to see the screen.
Right now the screen rotates, or with my face I am tapping on the screen." Three faults in the
lock of alpha.20, in the order a cheek finds them:

1. **The hold buttons were reachable through the lock.** The guard sat *under* the controls by
   design (a finger had to reach them), and a cheek resting on "hold to hang up" for 1.2 s is a
   finger to the browser. So the call ended, or the route flipped, by face.
2. **The page turned with the head.** The manifest says `orientation: any`, and a phone at an
   ear is tilted enough for Android to call it landscape.
3. **The browser's own bar was in reach.** Address bar, tab strip, the long-press text menu —
   none of it ours to guard.

The dialer has the proximity sensor for this and the web does not (docs/stories/10_voice.md,
platform truths). What the web has is gravity. A phone read is tilted back so the glass faces
the eyes — gravity comes out of the screen; a phone at an ear lies against a cheek, which is
vertical — gravity runs down the phone's length and almost none comes out of the glass. That
difference is `earPose.ts`: enter the ear pose inside ~12° of vertical and ~35° of upright,
leave it past ~22° of tilt toward a face; 450 ms to go dark (a cheek brushing past is not an
ear), 120 ms to come back (pulling it away must feel immediate); signs ignored, since Android
and iOS disagree on them and an upside-down phone at an ear is still at an ear; and a sensor
that stops delivering clears the dark within two seconds, so a black screen can never be stuck.

At the ear the call screen renders **nothing but black and a guard**: no controls exist to be
held. Away from the ear it is the locked screen of before, with two more cheek rules on the
holds: a contact wider than 60 px is not a fingertip and holds nothing, and a second contact
anywhere while a hold runs drops it (an ear comes with a cheek). `screenHold.ts` takes the call
**fullscreen and portrait-primary** inside the tap that starts it (orientation can only be
locked in fullscreen or an installed app) and lets both go when it ends; `-webkit-touch-callout`
and `overscroll-behavior` go off on the call surface. The pose has eight tests (`earPose.test.ts`)
from synthetic gravity vectors; 110 web tests pass.

Still true: only a native wrapper can turn the screen *off*. This is the closest the web comes,
and it is the dialer's behaviour as seen from the ear.


## 2026-09-19 — a voice of his own: OmniVoice on ardi's 3090 (core 2.0.0a54, proto a21, web alpha.31)

"Is it possible to have a local super nice TTS with nice Bulgarian voices — on ardi's 3090
where the STT is? I want local SOTA if possible." Research first: the strong open models do
not speak Bulgarian — XTTS-v2 (17 languages), Chatterbox (23), Fish/Qwen3-TTS, MOSS-TTS 1.5
(31, Macedonian in, Bulgarian out); Piper has one Bulgarian voice and it is not nice. One
candidate: **OmniVoice** (k2-fsa, the Next-gen Kaldi people, April 2026, Apache-2.0) — a
diffusion LM over a Qwen3-0.6B text encoder that clones any voice from a few seconds of it in
600+ languages, Bulgarian among them; 3.1 GB of weights.

Tested, not believed. `~/omnivoice-trial` on ardi, the card shared with Whisper: five
sentences (four Bulgarian, one English) through today's voice (edge-tts Borislav) and through
OmniVoice cloning that same Borislav from a 7-second clip of him, at 32/16/8 diffusion steps,
plus two voices *designed* from words alone ("male, middle-aged, low pitch"). Time per
sentence is flat, whatever its length: **1.75 s / 1.15 s / 0.55 s** for 4–6 s of audio, 1.9 GB
of VRAM resident. Then a judge that cannot be charmed: every sample back through the resident
Whisper large-v3, character error rate against the sentence it was asked to say — **edge 0.092,
clone32 0.092, clone16 0.094, clone8 0.103, designed 0.089**, the "errors" being 9 for девет
in all of them. Local Bulgarian is as intelligible as Microsoft's. Whether it is as *nice* is
Arsen's ear to say: `SharedFolderAI/jarvis-tts-trial/listen.html` has all six side by side.

So it is wired in, off by default:

- `scripts/omnivoice/` — a Dockerfile, a compose file (GPU by UUID, `:9120` on the tailnet
  IP and loopback), and `server.py`: the model resident in fp16, one voice-clone prompt per
  `voices/name.wav` + `name.txt` (rescanned, so a new pair dropped in the folder is a voice
  with no restart), `POST /tts {text, language, voice, steps}` → MP3, one sentence at a time
  on the card. Running on ardi as `omnivoice`, with `borislav` (bg) and `ryan` (en) — both
  clones of the edge voices, so switching engines changes where the voice is made, not who.
- `Settings.voice.engine`: `"edge"` | `"omnivoice"`, with `omnivoice_url`, `omnivoice_voices`
  per language (a name, or `design:…`) and `omnivoice_steps` (8/16/32 — fast/balanced/best).
  The synthesiser's cache key carries the engine, voice and steps; a sentence the local server
  cannot make (down, voice missing) is said by edge and not cached under the local key.
- Settings → Voice: "Where the voice is made", the three fields under it when local is on.

What this buys, beyond the sentence leaving the house or not: *any* voice. A clip and its
transcript in `voices/` and Jarvis speaks with it, Bulgarian or English. The clip should be
5–15 s, 24 kHz mono, one speaker, no music.


## 2026-09-19 — the seven hundred milliseconds after his last word (web alpha.32)

"Is it possible to have better/faster answers — audio streaming, not batches?" The reply
already streams: the run's sentences go to the voice one by one, the next fetched while the
first plays. The question is batch, and it has to be — Whisper recognises an utterance, not a
stream, and no streaming recogniser speaks Bulgarian. So the time between his last word and
the request leaving the phone was counted instead, and it was three waits in a row:

| after the last word | before | now |
|---|---|---|
| release: silence that closes the utterance | 750 ms | 750 ms |
| recognition (Whisper large-v3, resident) | ~700 ms | overlapped |
| join: silence that says he will not go on | 600 ms | 600 ms, counted from the end of speech |
| **total** | **~2,050 ms** | **~1,350 ms** |

Two changes. The segmenter now reports a **`pause`** 250 ms into a silence (`pauseMs`), the
listener sends the words so far to be recognised at once, and the session keeps the answer
under the pause's `at`; when the release confirms the end, `speech-end` carries the same `at`
and the recognition is already done or nearly — no second round-trip. If he talked through
the pause the end is a later silence, the `at` differs, the early answer is dropped unread and
the whole utterance is recognised afresh: one wasted request, never a wrong word. And the
**join window counts from the end of speech**, not from when the recogniser answered, so the
silence spent recognising is not paid twice. The hard deadline (1.5 s from the first held
words) and the noisy-room rule are as they were. Five new tests (segmenter: a pause once per
silence, none for a fragment; session: the pause that was the end, the pause talked through,
a failed early recognition, the window measured from speech-end); 115 pass.

What is left between the last word and the first spoken one is the model's first sentence
and the voice — 1.1 s of it OmniVoice at 16 steps, half that at 8.

## 2026-09-19 — Claude Code talks to Jarvis2, not V1 (core 2.0.0a55)

"Remove jarvis MCP from Claude and add jarvis2." The core's MCP app (`/mcp`, tools `jarvis_chat`,
`jarvis_status`, `jarvis_notes`) answered 421 Misdirected Request to the laptop — the SDK's
DNS-rebinding guard, the same one fixed in the host on 2026-09-05 and never in the core, since
until today nothing but localhost tests had called it. Off now, the bearer key being the
defence. A collab key named `claude-code` was minted for it (Settings → Collaboration shows
it); Claude Code's user-scope MCP config has `jarvis2` → `http://100.97.120.53:9020/mcp`
with that key, and the V1 `jarvis` entry is gone.

## 2026-09-19 — talking over him, and the call's quiet signals (core 2.0.0a56, web alpha.33)

Three complaints off one call at the ear: "докато ми говори не мога да говоря през него",
"второто ми съобщение все едно не беше разпознато и джарвис просто ми повтори първия отговор",
and "искам лек ненатрапчив сигнал ... нежно бип бип". The first two were the same fault seen
from both ends, and there were four of them:

1. **The gate learned the cut-in.** `EchoGate` let its echo peak follow any frame that was not
   already over the bar — so a voice rising into the room raised the bar under itself and could
   never be over anything. After the grace the peak may now rise by at most 6% a frame: the echo,
   already there, keeps it; a voice arriving into it cannot take it along.
2. **The gate kept judging a cut-in it had already accepted.** Once the segmenter has heard
   450 ms of voice over the echo the decision is made, so the gate now `latch`es open until the
   utterance closes. Before, his sentence was chopped into fragments under `minUtteranceMs` and
   dropped — he stopped Jarvis and then "said nothing".
3. **The bar ignored who was speaking.** With the server voice (and now OmniVoice) the page plays
   the audio itself, so the browser's echo canceller has already subtracted most of it: a
   `CANCELLED` profile (1.5× over the echo, not 2.5×) applies whenever `Speaker.cancellable()`.
   The device voice, which plays outside the browser, keeps the strict bar.
4. **The core dropped the words.** `steer` is read at the top of a step, and a run that answers
   has no next step: anything said while the final words were being written was appended to the
   control block and thrown away with it. Exactly the cut-in case, since he talks over an answer
   that is by definition the last step. The loop now takes pending steers before finishing and
   answers them in the same run (test: a steer landing mid-stream of the final turn).

And the answer he talked over is no longer thrown away on the guess that he meant to: it is
**held**. A cut-in that turns into words drops it for good and the steer carries a note saying
how much of it he actually heard ("of your 5 sentences he heard the first 2 — do not repeat
them"), which is why the model used to say the whole thing again. A cut-in that came to nothing
— a cough, a chair — resumes it, starting with the sentence he interrupted.

**The signals** (`tones.ts`): two soft notes up when his words go off to Jarvis, a quiet pair
every 2.6 s while he thinks (the first only after 1.4 s, so a quick answer is never announced),
two notes down when the socket drops and two up when it returns, and the screen says so too.
They play on the microphone's own AudioContext — the call's route, inside the echo canceller —
and each one hushes the microphone while it sounds, so a beep is never heard as a word.

121 web tests, 133 core tests.

## 2026-09-19 — a chat can talk to a chat (core 2.0.0a57, proto a22, web alpha.34)

"Искам Джарвис да може да си говори и синква с други активни сесии… да излиства активните
сесии и да си пише с @името" — the thing Claude Code does with its own sessions. A session here
is a **chat**: its own history, its own persona, its own idea of what is going on. Until today
they were sealed from each other and Arsen was the transport.

- **A handle is the title, slugged.** "Домо — етажна собственост" is `@домо-етажна-собственост`
  and answers to `@домо` while no other chat starts that way; two of one name are told apart
  oldest-first (`домо`, `домо-2`). Nothing is stored — the handle IS the title, so a rename
  renames the session and there is no second name to drift. An ambiguous `@дом` is refused with
  the candidates rather than guessed.
- **Three tools** (`features/sessions.py`): `sessions.list` (who is there, and which of them is
  working right now), `sessions.read` (the last messages of one, waking nothing), `sessions.say`
  (into another chat — a **steer** if a run is working there, a new run if it is idle, which is
  what "може да не е активна, но другата да я събуди" means). With `wait` it brings that
  session's reply back as the tool's result.
- **Only when Arsen says so.** His decision: Jarvis does not strike up conversations with
  itself. The tool says so, and every message lands visibly in both transcripts as
  `[@отсреща] …`.
- **The loop a feature like this owes an answer for.** A message carries the chain of
  conversations it has passed through (a ContextVar the engine sets per run, `engine/current.py`);
  a session already in the chain cannot be written to again, and the chain stops at
  `settings.sessions.max_hops` (2). Settings → Sessions has the switch, the chain limit and how
  long a reply is waited for.
- **Incognito is not a session.** Never listed, never read, never written to.
- **The web half**: `@` in the composer opens a menu of the chats (handles come from the core —
  `GET /api/sessions` — so the two ends never slug a title differently), Enter or Tab completes.
  A turn whose text contains an `@` carries the handles as per-turn context, so the model knows
  what `@домо` means without a round trip; the other ninety-nine turns pay nothing.

Two things the live core taught that the tests could not: conversations imported from V1 carry
**naive timestamps**, and sorting them beside today's aware ones took the whole session list down
with a TypeError (fixed, with a test); and of 259 conversations most are a scheduled run's own
chat named after its date, so the list offers plain chats and collab chats by name — 19 of them —
while any other can still be addressed by handle or id. A chat created WITH a name now keeps it:
the titler renamed "Тест сесия Б" after its first run and its handle moved under it.

## 2026-09-20 — the four seconds before he says anything (core 2.0.0a58, web alpha.35)

"Still not feeling like a regular human call… probably was not developed till the end." So the
turn was measured instead of guessed, on the live core, stage by stage — and the answer was not
where the last two days of work had been:

| between his last word and the first spoken one | was |
|---|---|
| release: silence long enough to be the end | 750 ms |
| Whisper large-v3 (overlapped from the pause) | ~1,050 ms |
| **Jarvis, before the request reaches the model** | **~1,050 ms** |
| vLLM's own time to first token | ~1,055 ms |
| the rest of the reply, because nothing is spoken until the step ends | 300–500 ms |
| OmniVoice, 16 steps | ~1,050 ms |
| | **≈ 5 s** |

Four cuts, each measured after:

1. **The skills detector was asking the model on every turn.** A trigger phrase literally present
   costs nothing; when none is, it spends a whole round trip on the classifier asking which skill
   applies — before the answer has begun. The planner was already skipped on a call for exactly
   this reason (a call is a conversation, not a plan); the detector now is too: it may look, it
   may not ask. Core time before the model: **1,050 ms → ~220 ms**.
2. **The voice is fetched while the model is still writing.** The words still wait for the step to
   end (a step that turns out to be tool calls wrote a plan, not a reply — 2026-09-16), but the
   *synthesis* of the first two sentences starts on the first delta. A second of OmniVoice moves
   out of the silence and into the time the model is still typing. Nothing said changes.
3. **Release 750 → 500 ms.** The quarter second was pure waiting; a sentence he carries on with
   after the close is no longer a lost turn, because the words that follow join the run as a steer
   (which the core learned to take even on its last step, 2026-09-19).
4. **Nothing starts beside a call.** The lanes are one engine on one pair of cards, and
   `run_routing` sends scheduled runs to the chat lane. A newsletter starting its prefill mid
   sentence took the model's first word from 1.0 s to 4.0 s — measured, twice. The dispatcher now
   holds every queued run that is not a call and not something Arsen typed until the call ends;
   they start the moment it does.

Measured again, idle, warm cache: first token **1.00 s** (vLLM's own 777 ms), whole short reply
1.22 s. The turn is now about **2.5 s** from his last word to the first spoken one, against ~5.
What is left is Whisper (~1 s on a 4.7 s clip) and the model's own prefill; the first call after a
restart still pays a cold prefix (4 s), which is the next thing to take.

## 2026-09-20 — a work tab per chat, and a tab that closes when the job is done (core 2.0.0a59, extension 2.2.0)

"Each chat session must use its own working browser tab. Right now they are competing for the
same tab when parallel chats use the browser… and close the working tab at the end of the job."

Both halves were true. The extension kept ONE work tab, from the days when one chat browsed at a
time (V1, 21 Aug: a tab per task left thirty tabs by evening). Worse, `targetTab()` preferred the
**active** tab — which is right for co-browsing ("I opened it for you") and exactly wrong with two
chats working: whichever opened a page last made it active, and the other chat then read and
clicked in it.

- **The call says whose it is.** `browser.call` now carries `session` — the conversation id, taken
  from the ContextVar the engine sets for every run (`engine/current.py`, added for the sessions
  feature two entries ago). An older core that sends none keeps the single shared tab it had.
- **One work tab per session**, kept in `chrome.storage.session` as a map (the MV3 worker unloads
  after ~30 s and a bare variable forgets everything). `browser.open` opens or reuses **this
  chat's** tab, and only brings it to the front when it is created — with several chats browsing,
  every `open` stealing the foreground made the browser flicker between their tabs.
- **Co-browsing survives.** The active tab is still used first when it is the user's own or this
  chat's; a tab that belongs to ANOTHER session is never a target. `browser.tabs` marks each one:
  "this chat's work tab", "another chat's work tab — leave it alone".
- **The job ends, the tab goes.** The engine gained an after-run hook; the core tells the
  extension `browser.job_done` for a chat that actually browsed, and the extension closes that
  tab after a **three-minute grace** (a follow-up turn a few seconds later still finds its page).
  Any new call for that chat cancels the close. It refuses to close a tab the user is looking at
  when the grace runs out — that tab is his now — and blanks rather than closes a window's last
  tab.

Three tests in the extension (two chats, two tabs, neither driving the other's; the grace and the
close; a chat that browses again keeping its tab) and one in the core (the call carries the
session, and `browser.job_done` follows the run). 88 extension tests, 149 core, 128 web.
