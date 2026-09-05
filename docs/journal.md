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
