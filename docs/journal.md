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
