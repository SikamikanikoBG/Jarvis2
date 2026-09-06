# Full process audit — 2026-09-06

Brief: audit every process end to end, fix what is broken, and strengthen the unit +
regression suites so each fix ships with the test that would have caught it.

## Before state (measured, not assumed)

```
git:      main @ 24b8c1a "docs: attachments verified live (image described, CSV answered)"
version:  jarvis-core 2.0.0a8 / jarvis-web 2.0.0-alpha.1
python:   uv run pytest -q                -> 187 passed
web:      npm run test -- --run           -> 36 passed (5 files)
lint:     uv run ruff check packages      -> All checks passed
types:    uv run pyright packages         -> 0 errors, 0 warnings
coverage: 83% overall (8567 statements, 1474 uncovered)
```

Weakest modules by line coverage at the start:

| module | cov |
|---|---|
| `host/audio.py` | 26% |
| `host/screen.py` | 45% |
| `host/status.py` | 48% |
| `core/api/features.py` | 49% |
| `core/api/media.py` | 50% |
| `core/features/boards.py` | 57% |
| `core/features/collab.py` | 68% |
| `core/features/knowledge.py` | 69% |
| `core/features/skills.py` | 73% |
| `core/features/schedules.py` | 75% |

Coverage is a map of where nobody has looked, not a defect count. The audit reads the
processes; coverage only says which rooms are dark.

## Processes in scope

1. Agent loop / run engine / supervision (the thing every other process runs on)
2. Context assembly + prompt-cache prefix discipline
3. Schedules (cron, catch-up, dry runs)
4. Triage (mail routing)
5. RSVP (calendar invites)
6. Meetings (capture -> transcribe -> summarise)
7. Attachments (images, PDFs, pasted text)
8. Knowledge graph + boards + skills
9. Tools: registry, MCP provider, WS provider, facades
10. Model adapters (ollama, openai-compat, http)
11. Host: Outlook, OneNote, files, shell, audio, status
12. HTTP surface: rest, ws, media, features, collab, openai-compat

## Findings

Each one shipped with the test that fails without the fix. The "verified" column is how the
defect was demonstrated on the *old* code before it was changed.

| # | Process | Defect | Verified by |
|---|---|---|---|
| 1 | packaging | `import jarvis_core.features.knowledge` (or `.skills`) FIRST raised a circular ImportError; only `app.py`'s import order hid it | 2 of 46 modules fail the new import test |
| 2 | model adapters | `endpoint_semaphore` compared free permits, not the configured limit: roles stopped sharing a limit while idle, and a *raised* `max_concurrent_runs_per_endpoint` was ignored for the life of the process | semaphore identity assertions |
| 3 | attachments | a resized RGBA image was re-encoded to JPEG and still called `image/png` — the model got `data:image/png;base64,/9j/…` | magic-number vs mime, 4 shapes |
| 4 | attachments | the resize was taken only if the file also got smaller, so a flat screenshot kept its full resolution — measured 268 kB PNG vs 771 kB resized JPEG | image asserted scaled to 1568 px |
| 5 | RSVP | a failed `calendar_respond` was written to the ledger, so the invite was never answered again; `report.errors` empty, `last_error` cleared | retry → answer → not twice |
| 6 | triage | a failed `outlook_move` left a decision row claiming the move AND advanced the cursor past the mail; `report.errors` empty | retry test + summary text |
| 7 | triage | `report.routed` was assigned the day's total inside the per-account loop, so the last account overwrote the rest | two accounts, 4 vs 0 |
| 8 | knowledge graph | `graph(center=X)` trimmed a `set` with `list(ids)[:limit]`, so X itself was usually dropped | centre missing 20/20 at limit=5 |
| 9 | knowledge graph | `kg.remember` created an entity named `"?"` for a relation with an empty target | no `?` node, one edge |
| 10 | meetings | `stop_all` cancelled pollers but not transcribers, which kept writing into a closing DB | old code logs "Cannot operate on a closed database" |
| 11 | schedules | "Run now" never recorded a fire: no history, `last_run_id` stale | fires() + last_run_id |
| 12 | web/api | subscribing marked a conversation read and announced nothing, so other clients kept the badge | ping-fenced WS test |
| 13 | api | `?limit=-1` meant "no limit" to SQLite on five endpoints | `_clamp` at both ends |
| 14 | api | bearer tokens compared with `==` on REST, WS and collab (the host already used `hmac.compare_digest`) | shared helper |
| 15 | web | the composer cleared the textarea before the store accepted the message, so a send refused for being offline or for a run in flight deleted what was typed — the second case said nothing at all | `selectSendRefusal`, 4 cases |
| 16 | agent loop | the "ephemeral" plan trailer was popped only when it was still last, which no tool-calling step leaves it — so every such step buried a stale plan block in the context, each marking a different step "← current" | "call 1 carried 2 plan blocks" |
| 17 | host | `MeetingCapture.close_all()` existed, was tested, and was never called: shutdown kept the microphone and loopback device | `[] == ['mic','system']` |
| 18 | agent loop | `record_idempotent_result` stored every mutating result and nothing read it back; a duplicate call was told "duplicate call refused", which reads as a failure for an action that DID happen | replay test |
| 19 | api | `/api/openapi.json` and `/api/docs` answered without a token — the whole API description, on a box reachable over Tailscale with `/collab` tunnelled publicly | route-table walk |

### Looked at and found sound

Recorded so the next reader does not re-open them.

* `kg_aliases` is `COLLATE NOCASE` in the schema, so the alias lookup in `find()` is
  case-insensitive despite the SQL not saying so — checked before "fixing" it.
* `Files.resolve` is not bypassable by a symlink: `Path.resolve()` follows the existing prefix,
  so both a symlinked target and a symlinked parent land outside the roots and are refused.
* A path traversal in a skill name cannot reach the route — the router normalises `..%2F` away
  before matching — and `SkillStore._path` refuses it anyway.
* `EmailPolicy.is_approved`'s domain rule keeps the `@`, so `@bank.bg` does not match
  `attacker@evilbank.bg`. `Confirmations.needs_confirmation` skipping `always_ask` for unattended
  runs is deliberate and documented; the draft-instead-of-send policy is the real guard there.
* `search()`'s DASL `LIKE` does not escape `%`, but every row is verified against the query
  afterwards (the V1 Gmail incident), so an over-match costs rows, not correctness.
* The host's `BearerAuth` was already constant-time; `ComWorker`'s abandon-on-timeout contract
  holds (a timed-out caller never parks a thread on a lock).
* `registry.call`'s double timeout (outer `wait_for` and the provider's own) races, but both
  paths produce a "timed out" result, so it is cosmetic.
* `MeetingCapture.pull()` consuming a chunk before the `after_seq` check could lose audio only
  if the core asked with a seq it already holds, which the pull protocol makes unreachable.
* Every other place that clears an input after an action (boards, notes, collab keys) already
  clears inside `.then()`; the composer was the only one that cleared first.

### Known and deliberately left

* `regenerate()` re-sends the last user message without its attachments. Fixing it properly
  needs attachments to be shared between messages (today one row points at one message), so
  re-binding would take the picture off the original message. Out of proportion here.
* `TriageSettings.rules_for` replaces a mailbox's rules wholesale, so an account override that
  only wanted `demand_routing: false` also loses the global alerts. That is a design decision,
  not a defect — but it is a footgun worth revisiting.

## After

```
python:   255 passed (was 187)
web:      40 passed (was 36)
lint:     ruff + pyright + eslint + tsc all clean
coverage: 87% (was 83%) — api/features 51→97, api/attachments 45→88, boards 57→86,
          schedules 75→88, skills 73→81
live:     core booted on a temp home, 37 tools, /api/health 200; the Run-now fix, the negative
          -limit clamps and the docs gate verified against the running server, not only in tests
```

Ten commits, each carrying the test that fails without it.

