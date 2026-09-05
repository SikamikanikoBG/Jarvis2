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
