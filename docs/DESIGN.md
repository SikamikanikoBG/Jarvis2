# Jarvis V2 — Design

Status: **decided 2026-09-05** (Arsen: "go — start building"). Open items are listed at the
end with the decision taken; change them by editing this file, not by drifting.

## 1. Why a V2

V1 (`R:\Projects\Jarvis`, v1.310.0) works but is patchwork. Measured, not guessed:

| | V1 |
|---|---|
| Python | 114k LOC in `jarvis/` + **37k LOC of tools at repo root** = ~151k |
| Web UI | one 15,542-line HTML file: 3.5k CSS, 0.4k markup, 11.6k vanilla JS |
| Server | one 8,196-line `server.py`, 160 routes |
| Agent loop | `run_agent()` is **one 3,175-line function** wrapping LangGraph's `create_react_agent`, plus a monkey-patch on `langchain_openai` to keep the `reasoning` delta |
| Concurrency | 89 `threading.Thread` sites, 6 separate asyncio loops, a `queue.Queue` as the foreground serializer, 5 different hand-off paths into the agent |
| UI state | 213 globals, 192 `window.X =` exports, 275 inline `onclick=`, 176 `innerHTML` writes; one smart-quote broke the app twice |
| Data | schema v36, 52+ tables, ~12 of which back features we keep |
| Settings | 110 top-level keys, 13 backup copies beside the file |
| Deps | 205 locked packages, 10 LLM providers, LangChain + LangGraph |
| CI | deploys on every push, **never runs pytest** |
| Tests | 47k LOC, but only ~29 real browser tests, all on email flows |

Most of the bugs Arsen chased in the last months lived in seams between the framework and
the app: Stop that didn't stop, checkpoints poisoned by thinking blocks, guards that armed
493 times and were consumed 0 times, a supervisor whose verdicts were ignored, schedulers
double-firing across two instances, silent model fail-overs. V2 removes the seams.

## 2. Scope

**Keep:** Ollama + vLLM inference · notes boards · knowledge graph · tools · plan-then-execute ·
web UI · browser side panel · STT · meetings (recorder + screen capture) · multi-inbox ·
demand auto-routing · background triage · multi-host · remote memory · **scheduled prompts**
(replaces reminders/scheduler) · config sync · agent collab · MCP.

**Cut:** TUI · cloud LLM providers · cases · journal lanes · grounding gate · self-improvement
loop · benchmark framework · image generation / vision · setup wizard · PyPI packaging ·
Langfuse · HomeLab Monitor integration · RAG/semantic email search · TTS · wake word.

**Requirements:** super fast backend · web UI only · super efficient code · an agent that can
run long tasks · a *really robust* harness ("think Claude Code") · excellent UI.

## 3. Shape: one brain, thin hands, one protocol

```
                 ┌──────────────── core (ardi, Docker, Python 3.12 asyncio) ────────────────┐
 phone/laptop    │  FastAPI ── /ws (events) ── /api (CRUD) ── /mcp (collab) ── SPA static    │
 browser ◄──────►│  RunEngine ── AgentLoop ── ContextProviders ── ToolRegistry ── Scheduler   │
                 │  SQLite (WAL)  ·  model adapters: Ollama(native) · vLLM(OpenAI-compat)     │
                 └───────┬────────────────────┬─────────────────────────┬────────────────────┘
                         │ MCP (streamable HTTP)│ WS tool-provider        │ HTTP
                 ┌───────▼───────┐    ┌────────▼────────┐      ┌─────────▼────────┐
                 │ jarvis-host   │    │ browser ext.    │      │ whisper · ollama │
                 │ (Windows)     │    │ (MV3, Brave)    │      │ · vLLM           │
                 │ outlook.*     │    │ browser.*       │      └──────────────────┘
                 │ meeting.*     │    │ side panel =    │
                 │ screen.* fs.* │    │ SPA in iframe   │
                 └───────────────┘    └─────────────────┘
```

Three programs and one protocol:

1. **core** — the only stateful process. Owns the DB, the config, the schedules, every model
   call and every run. Runs on ardi in Docker (can run anywhere; exactly one per brain).
2. **host** — a thin Windows daemon exposing the machine's local capabilities as an **MCP
   server**: Outlook COM (multi-store), meeting audio capture (mic + WASAPI loopback),
   foreground-window screen grabs, local files, shell. No brain, no DB, no LLM. Several
   hosts may register with one core — *that is* "multi-host".
3. **web** — one TypeScript SPA served by core, mobile-first PWA. The browser extension loads
   the same SPA in its side panel (`?mode=panel`) and adds `browser.*` tools over the WS.

Consequences that make three V1 features structural instead of modules:

- **Remote memory** = the core is remote. No SQL proxy, no `_remote` switch, no schema-version
  handshake between copies.
- **Config sync** = there is one config, in core. Per-machine settings (Outlook profile, audio
  device) live on the host that owns them and are never synced.
- **Multi-host** = N MCP servers registered in core's `hosts` table.

### Why still Python

The bottleneck is model inference and I/O (COM round-trips, network), not CPU. Rust/Node buy
nothing there and cost the ecosystem that the hands need: `pywin32` for Outlook, `pyaudiowpatch`
for loopback, `faster-whisper`, and the reference `mcp` SDK. Speed comes from architecture:
a single asyncio loop, no threads except COM, no framework layers between us and the model,
a streaming-first protocol, and a prompt prefix designed for the KV cache.

Performance targets (measured in CI with a fake model, and in the status page for real ones):
event fan-out < 20 ms; core overhead before first token < 100 ms (excluding the model's own
TTFT); context assembly < 150 ms with no LLM call on the hot path except one optional
parallel pre-flight classifier.

## 4. The harness — own it, borrow the plumbing

Question settled: **we write the agent harness ourselves; we do not write the plumbing.**

Options weighed:

| | Verdict |
|---|---|
| LangGraph (V1) | The seam that produced the worst bugs. Its message format and checkpointer are its own; thinking blocks poisoned checkpoints; cancellation is awkward; ten providers of abstraction for two OpenAI-shaped endpoints. No. |
| Pydantic AI | The strongest library option: typed tools, MCP client, OpenAI-compat models. But Ollama only via the compat layer (loses `think`/`num_ctx`/`keep_alive` — three hard V1 lessons), its own message history type would again be what we persist, and durable execution means Temporal/DBOS. Not for a single-user system. |
| OpenAI Agents SDK | Same shape of trade-off, more OpenAI-specific. No. |
| **Own loop** | The loop is ~500 lines when there are exactly two OpenAI-shaped backends. Small enough to test every branch with a scripted fake model. Every V1 failure mode becomes a mechanism we control. **Yes.** |

Borrowed and never rewritten: `mcp` (client + FastMCP server), `ollama` and `openai` SDKs
(transport only), `pydantic` v2 (every schema), `fastapi`/`uvicorn`, `aiosqlite`, `croniter`,
`httpx`. Web: React 19 + TypeScript + Vite, `zustand`, `marked` + `DOMPurify`.

### 4.1 The Run

Everything the agent does is a **Run**: a chat turn, a scheduled-prompt fire, a collab
request, a triage cycle, a meeting summary. There is no other way to make the model do work.

```
Run
  id, conversation_id, kind ∈ {chat, scheduled, collab, triage, meeting, system}
  status ∈ {queued, running, waiting_user, cancelling, done, failed, cancelled, interrupted}
  input (text + attachments), plan (optional), budget, priority, usage, started/finished
RunEvent (append-only, seq-numbered)
  run.queued · run.started · plan.created · plan.step_started · plan.step_done
  model.call · model.delta(text|reasoning) · model.done(usage)
  tool.call · tool.result · tool.confirm_requested · tool.confirm_resolved
  guard.armed · guard.consumed · judge.verdict
  run.waiting_user · run.resumed · run.done · run.failed · run.cancelled · run.interrupted
```

`run_events` is the source of truth for what happened; `runs` is the materialised head. The
UI's Run Inspector renders `run_events` directly — that replaces Langfuse.

### 4.2 The loop

```
create_run → queue (priority, per-endpoint concurrency)
  → assemble context (providers, stable prefix first)
  → [pre-flight: one cheap parallel call classifies tier + skills + KG routing]
  → [planner if the pre-flight says "multi-step": plan persisted, one step at a time]
  → model.call (stream) → text/reasoning deltas fan out over WS
      → tool calls: validate args (pydantic) → policy (read-only? confirm?) → dispatch
        (MCP / WS provider / builtin) with per-call timeout → ToolResult(kind, text, cursor…)
      → repeat until the model stops calling tools
  → step done? next step / replan / finish
  → supervision at every boundary (budgets → structural signals → judge → **engine acts**)
  → final reply persisted; run.done
```

Rules that are code, not prompt:

- **Cancel actually cancels.** One `asyncio.Event` per run, checked before every model call and
  every tool call; the model stream is closed; in-flight tool calls receive cancellation.
  The Stop button emits `run.cancel`; the engine answers with `run.cancelled` only after the
  task has really exited. There is no other stop mechanism.
- **Resume after restart.** On boot every `running` run becomes `interrupted`, then resumes
  from its last completed boundary. An in-flight tool call is reported to the model as
  interrupted. If that tool was **not read-only** (MCP `readOnlyHint=false`), the run goes to
  `waiting_user` instead of resuming blind — no double-sent email.
- **Idempotency keys.** Every mutating tool call carries `run_id:step:seq`; the host refuses to
  execute the same key twice.
- **Budgets per run kind** (steps, tokens, wall-clock). Exhaustion ends the run with a summary
  event, never with silence.
- **Supervision with power.** Structural signals (identical tool call repeated, tool-error
  streak, no new information in N steps) *summon* a judge call (cheap role, thinking off).
  The judge's verdict — `continue | nudge | stop` — is executed by the engine. A verdict is an
  event; a guard that arms emits `guard.armed`; the test suite asserts every guard is consumed
  in at least one scenario. No stacked counters, no guard nobody can see.
- **No silent fail-over anywhere.** A fallback (model, STT backend, host) is an event and a
  badge in the UI. V1 served a backup STT model for three days while the UI said otherwise.
- **Thinking is a per-role switch enforced by the adapter.** Roles: `chat`, `planner`,
  `classifier`, `judge`, `triage`. Only `chat` may think. V1 rediscovered "a thinking model
  given 48 tokens spends them all thinking" three times in five days.
- **Timeouts and retries** live in the adapters: connect/read timeouts, retry with backoff on
  connection errors and 5xx, never on 4xx; malformed tool-call JSON is returned to the model
  as a tool error once, then counts as a structural signal.
- **Reasoning is stored beside the message, never re-sent.** Our own `Message` type is what
  we persist; adapters translate per call. Nothing an upstream library changes can poison
  history.

### 4.3 How it is tested

The loop is small enough to be exhaustively tested with a **scripted fake model** (a list of
turns: text, tool calls, malformed calls, slow streams, disconnects). Required coverage:
cancel during stream, cancel during tool, restart during read-only tool (resumes), restart
during mutating tool (waits), budget exhaustion, judge stop, repeated call, MCP server dies
mid-call, malformed JSON, resume of a planned run at step 3. Playwright drives the SPA against
the real core with the fake model in CI. The real-model, real-Brave gate stays a local rule
before any version bump ([[feedback_e2e_before_version_bump]]).

## 5. Conversations

Every run lives in a conversation. `conversation.kind ∈ {chat, scheduled, collab, triage,
meeting, archive}`, plus `folder_key` (schedule id, collab key, meeting id) so the sidebar
groups them:

```
Chats                       ← flat, newest first
Scheduled ▸ Morning brief   ← one sub-folder per schedule, runs by date inside
          ▸ Revolut radar
Triage    ▸ 2026-09-05      ← one conversation per day per account
Meetings  ▸ 2026-09-04 Steering committee
Collab    ▸ claude-code
```

Compaction: when a conversation approaches the working budget, older turns are summarised by
the `classifier` role into a single `summary` message; the summary is part of the stable
prefix. Tool results older than the current step are truncated to their first lines (V1's
`_compress_old_tool_messages`, kept because it worked).

## 6. Tools = MCP everywhere

`ToolProvider` is one interface with three implementations: `McpProvider` (streamable HTTP or
stdio — hosts and external servers like `fetch`), `WsProvider` (the browser extension), and
`Builtin` (core-owned: `notes.*`, `kg.*`, `schedule.*`, `jarvis.*`). The registry unions them
into namespaces.

**Exposure policy.** V1 learned that the local model works best with ~10-15 tools, hence the
14 facades. V2 keeps the idea and removes the defect: a facade per namespace is **derived from
the live tool list** (`op` enum + per-op arg schema in the description), so an op cannot exist
without a backing tool. Per-namespace override `expose: facade | flat`.

**Tool result contract.** `ToolResult(kind ∈ {data, empty, partial, error}, text, count, total,
cursor, error)` — V1's type, adopted everywhere from day one. A `partial` result the model never
paged is a structural signal before the run ends (V1 "check 6").

**Policy.** MCP annotations drive behaviour: `readOnlyHint` (safe to resume/retry),
`destructiveHint` (confirm unless the run kind is unattended *and* the tool is on the
allow-list). Confirmation is `tool.confirm_requested` → UI card → `tool.confirm_resolved`.

## 7. Context providers

The system prompt is assembled from providers in a **fixed order designed for the KV-cache
prefix**: static prompt (identity, rules, tool usage) → notes boards (change rarely) →
skills (detected for this message) → KG facts (per message) → conversation. Each provider is
`async contribute(RunContext) -> ContextBlock | None` with a token cap; none may call the model
on the hot path — the one pre-flight classifier call runs in parallel with assembly and feeds
skills + KG routing.

Skills stay markdown files in core's `skills/` (V1's 39 files import as-is). The V1 rule set
lesson holds: the number of instructions, not their length, is what degrades the local model,
so the static prompt is one numbered rule list behind a precedence block, target ≤ 40 rules.

## 8. Scheduled prompts

```
schedules(id, name, cron, tz, prompt, role, tools_policy, enabled, catch_up ∈ {skip, run_once},
          last_fired_for, next_fire)
```

Fire = create a `scheduled` conversation (`folder_key = schedule.id`) + one run. `UNIQUE
(schedule_id, scheduled_for)` makes double-fire impossible; with one core there is no second
instance to race anyway. "Run now" is the same path with `scheduled_for = now`. Reminders are
gone: "remind me at 15:00" is a one-shot schedule whose prompt is "tell Arsen: …".

## 9. Email

The host exposes `outlook.*`: `accounts`, `folders`, `list(folder, since, cursor)`, `read`,
`search`, `move`, `flag`, `send`, `reply(reply_to_entry_id)`, `calendar.*`. Implementation
rules carried from V1: `Folder.GetTable` for every listing (5-34x faster, accepts filters
`Restrict` rejects), never trust `FlagStatus` alone, Bulgarian Outlook → resolve default folders
by `GetDefaultFolder` id not by name, short-term EntryIDs from tables, one COM thread with
per-call timeouts and no zombie waiters.

**Triage** is a built-in scheduled job in core: `outlook.list(since=cursor)` → `triage` role
classifies each item (thinking off, small ctx) → deterministic **demand routing** first (a
`DM-1234` literally present in the subject/body is a structural precondition; the model decides
only the ambiguous rest) → `outlook.move` → the day's triage conversation gets one compact
message per batch. Cursor and decisions are persisted so a crash re-runs nothing.

Backend decision: **COM via host only** for V2.0 (the laptop must be on for triage). V1's Graph
backend (962 LOC) is a later optional provider if 24/7 triage is wanted.

## 10. STT and meetings

- **Chat STT:** browser `MediaRecorder` → `POST /api/stt` (webm) → core → the existing Whisper
  service (ardi `:9110`). No `pyaudio` in the chat path. Language list is a setting; a backend
  or model fallback is an event + badge, never silent.
- **Meetings:** `meeting.start(device)` on the host mixes mic + loopback (V1
  `meeting_recorder.py`, ported) and streams chunks to `POST /api/meetings/{id}/chunks`; core
  transcribes and appends segments to a `meeting` conversation live; `meeting.stop` triggers a
  summary run. **Screen capture** on the host grabs the foreground window on a timer; with
  vision cut, frames are selected structurally (scene-change hash) and OCR'd (tesseract) into
  the timeline; selected frames are attached to the summary as images.

## 11. Collab

Core is itself an MCP server at `/mcp` (`jarvis.chat`, `jarvis.status`, `jarvis.notes`) and
keeps `POST /api/collab/message`. Each key owns a `collab` conversation. Public exposure stays
the Cloudflare quick tunnel. Bearer keys hashed at rest.

## 12. Web UI — rebuild, salvage the design

Rebuilding is the efficient choice: the protocol changes to typed run events, so 59% "kept"
JS would still be rewired line by line; the file has no modules, DOM-as-state and 275 inline
handlers; and there is no test coverage to protect a refactor. What is **salvaged**: the 3.5k
lines of CSS tokens and patterns (dark/light, cards, sheets, lists — Arsen's design language,
see `PRODUCT.md`), the `panel-mode` iframe approach for the extension, the PWA manifest, the
email-triage and boards UX as screen specs, and the mic-capture code.

Stack: React 19 + TypeScript + Vite; `zustand` store fed by one WS event reducer; CSS
variables (no Tailwind); `marked` + `DOMPurify`; Vitest for reducers, Playwright for screens.
Types for the protocol are generated from the pydantic models (`jarvis_proto` → JSON Schema →
TS) and snapshot-tested on both sides.

Screens (v2.0): Chat (stream, reasoning fold, tool cards, confirm cards, Stop) · Sidebar with
folders (§5) · Run Inspector · Boards · Knowledge · Schedules · Triage · Meetings · Hosts &
Integrations (MCP) · Settings (generated from the settings schema) · Status.

## 13. Data model (16 tables, was 52+)

`conversations` · `messages` · `attachments` · `runs` · `run_events` · `schedules` ·
`schedule_fires` · `note_boards` · `sticky_notes` · `kg_entities` · `kg_aliases` · `kg_edges` ·
`kg_mentions` · `hosts` · `mcp_servers` · `collab_keys` · `settings` (one row per key — PATCH
semantics end the whole-document race V1 fixed in v1.287.0) · `meetings` · `triage_state` ·
`triage_decisions`. SQLite WAL, numbered SQL migrations, `aiosqlite`.

## 14. Repository

```
Jarvis2/
  packages/proto/     jarvis_proto   pydantic models: Message, ToolCall, ToolResult, events, settings
  packages/core/      jarvis_core    FastAPI app, RunEngine, AgentLoop, adapters, providers, db, scheduler
  packages/host/      jarvis_host    FastMCP server: outlook, meeting, screen, fs, shell (Windows)
  web/                React SPA
  extension/          MV3 extension (V1 pageKernel ported, new transport)
  docs/               DESIGN.md, stories/, journal.md
  .github/workflows/  ci.yml (ruff, pyright, pytest, vitest, playwright, docker build), deploy.yml
```

uv workspace; `ruff` + `pyright --strict`; **pytest runs in CI**; the Docker image is
multi-stage (node build → python runtime). Versioning: `2.0.0-alpha.N` until cutover.

**Size budget:** core ≤ 12k LOC, host ≤ 4k, web ≤ 8k TS, extension ≤ 4k (mostly ported), tests
at least as large as the code. ~28k against V1's ~170k.

## 15. Delivery

Each phase ends with a milestone that Arsen can use, and stories are written before the
phase is implemented ([[feedback_user_stories]]). Repetitive ports (Outlook COM tools, CSS
port, extension transport) go to the local intern via Aider ([[feedback_delegate_to_intern]]);
the harness itself does not.

| Phase | Delivers | Milestone |
|---|---|---|
| 1 Harness + chat | proto, db, adapters (Ollama, vLLM, fake), RunEngine, AgentLoop, WS/REST, SPA chat + sidebar + Run Inspector, CI | chat with qwen on vader from the phone; Stop stops; kill the core mid-run, restart, the run resumes |
| 2 Context | notes boards, KG (ported), skills, compaction, pre-flight classifier, planner | boards and KG visible and injected; a 20-step planned task completes |
| 3 Tools | MCP client + exposure policy + confirm flow; host daemon with `outlook.*`, `fs.*`, `shell.*`; `fetch` server | "reply to Rumen's mail from yesterday" end-to-end from the phone |
| 4 Schedules + triage | scheduled prompts + folders UI, triage job, demand routing | morning brief fires into its folder; inbox triaged with zero manual steps |
| 5 Browser | extension transport port, `browser.*` provider, side panel | LinkedIn page driven from the side panel |
| 6 Voice | STT endpoint + mic UI; meetings (host capture, live transcript, summary, screen frames) | a recorded meeting produces a summary with frames |
| 7 Cutover | collab MCP server, auth/pairing, deploy on ardi, data import (boards, KG, skills, schedules), V1 archived | V1 container stopped |

## 16. Decisions taken (change here, not in code)

1. Harness: **own loop**, plumbing borrowed (§4).
2. Frontend: **React + TS + Vite**, V1 design tokens ported; V1 file retired.
3. Core placement: **ardi Docker is the one core**; laptops run only `jarvis-host`.
4. Skills: **kept** as markdown context providers.
5. Email backend: **COM via host** for 2.0; Graph later if 24/7 triage is wanted.
6. Meeting frames without vision: **scene-change + OCR**.
7. TTS, wake word, RAG: **out**.
8. Calendar: **in**, part of `outlook.*`.
9. Repo: **new repo `Jarvis2`**; V1 stays as reference until cutover.
10. V1 conversations: **imported read-only** under an `Archive` folder; everything else re-created.

## 17. Risks

- The local model's tool-calling quality with derived facades — mitigated by the `flat`
  override per namespace and the planner's one-step-at-a-time prompts.
- Outlook COM on a host that sleeps — accepted for 2.0 (decision 5).
- One core = one point of failure — accepted; it is a container with a health check and a
  volume; V1 already ran this way on ardi.
- Rebuilding the UI costs Phase 1 time — bounded by salvaging the CSS and keeping v2.0 to the
  eleven screens above.
