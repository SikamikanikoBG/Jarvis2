# 04 — Panel: architect's answers to the rebuttal (challenge round, 2026-10-29)

Code stayed read-only. I made one read-only GET of the live settings
(`ssh ardi … curl 127.0.0.1:9020/api/settings`) because challenge 6 asked for the live value.

## Facts checked first (they change the answers)

| Claim | Code / live | Verdict |
|---|---|---|
| browser.click/type/eval are `destructive:false` | Yes. The annotations come from the **extension**, not the core: `extension/background.js:124` (click), `:142` (type), `:211` (eval), `:166` (upload). Open and scroll are `read_only:false` at `:78/:182`. The core takes them as sent (`tools/ws_provider.py:146-160`, `ToolSpec.model_validate(raw)`). | **True** |
| An `openWorldHint` exists to use | No. `ToolSpec` (`proto/tools.py:18-27`) has only `read_only/destructive/idempotent/internal`. MCP maps only `readOnlyHint` and `destructiveHint` (`tools/mcp_provider.py:158-159`). | The proposed mechanism needs a proto change and every provider to annotate correctly |
| shell_run is ungated | `shell_run`, `fs_write` and `fs_edit` are `DESTRUCTIVE` (`packages/host/jarvis_host/server.py:4, 503, 509, 522`). But… | see next row |
| **Live confirmations** | `{"mode":"off","always_allow":[],"always_ask":["*.outlook_send"],"outbound_check":["browser.click","browser.type"]}`. **mode=off in production.** Today nothing destructive asks in any chat, only outlook_send. `outbound_check` is a stored key **no code reads** (grep: 0 hits, no git history). It looks like an intent to gate click/type that was never implemented. | Challenge 1 is more serious than stated |
| Live `voice.style` | **Edited by Arsen**: the old default plus his appended English-only and number-reading paragraph. It **does contain** the exact sentence `You have all your tools on the call: do what he asks now, never "after the call".` | D's "equal to a previous default" swap would be a no-op on live. A's "remove the exact sentence" works on live |
| `sessions.say` creates CHAT runs | No. `_deliver` creates **COLLAB** runs (`features/sessions.py:341`). Collab and openai_compat are COLLAB too (`features/collab.py:95`, `api/openai_compat.py:73`). CHAT runs come only from ws `run.create` / the steer fallback (`api/ws.py:100,119`). The real gap is different: `_deliver` can **steer into a working CHAT run** (`sessions.py:335-337`). | Partly wrong, but there is a real gap |
| (from D) stored `budgets`/`run_routing` lack TASK | True. Per-key storage (`db/store.py:605-618`). The budget silently falls back to the 25-step default (`run_engine.py:196`). | My §5 missed this. I adopt D's "kinds added later get their default" validator (`settings.py:764-773` pattern) |

## Answers

### 1. Browser and outward actions — **accept with change** (same goal, different mechanism)

I reject the `openWorldHint` extension change as the gate:
- It needs a new `ToolSpec` field.
- It needs an extension redeploy into Brave.
- It still trusts every provider to annotate its own tools correctly. The homelab or any future MCP server's mutating tools have no hints today.

The safe gate is the core's own rule for TASK runs.

- **Every non-read-only tool call in a TASK run waits for approval**, except names on `settings.tasks.no_approval`.
  - The default list is: `files.attach`, `task.*`, `notes.*`, `kg.*`, `browser.open`, `browser.scroll`. These cover navigation and Jarvis's own memory, not actions on the outside world.
  - `browser.click`, `browser.type`, `browser.eval`, `browser.upload`, all `*.outlook_*` writes, `mail.*` writes and every unknown MCP mutation therefore ask.
  - The rule is settings data, not a word list.
  - Implementation: `needs_confirmation(..., strict=run.kind is TASK)` in `proto/settings.py:463`. In strict mode, `mode="off"` and `always_allow` are ignored and `destructive` becomes `not read_only`. It is called from `engine/loop.py:810-822`, with `spec.read_only` passed in.
- **Excluded from TASK runs in v1**: `settings.tasks.deny_tools = ["*.shell_run", "*.fs_write", "*.fs_edit"]`.
  - These are hidden in `_exposed_tools` (`loop.py:195`) and refused again in `_execute_one` (`loop.py:760` pattern).
  - Under the strict rule they would ask anyway. Removing them takes away the long-unattended-shell class of problem entirely (2026-10-11, 2026-10-15).
- I keep the strict rule (A §8), not D's.
- Tests:
  - a task that does `browser.type(submit=true)` ends `waiting_user`, and the card shows the arguments;
  - `browser.click` asks even with live-shaped settings (`mode=off`);
  - `shell_run` is not offered and is refused if named.
- Side note for Arsen: live `mode=off` plus the dead `outbound_check` key mean **his ordinary chats** do not gate click/type either. That is out of scope here, but it should be flagged.
- Cost: research tasks that need clicks (pagination, "accept cookies") will pause. Measure on the alpha. If it is too chatty, Arsen can add names to `no_approval`. That is his explicit trust, in data.

### 2. Evidence nobody checks — **accept with change**

- `task.finish` DoD items become `{item, met, refs: [tool_call_id | attachment_id], outward: bool}`.
- TaskService verifies in code, against the task conversation's own messages (`store.list_messages(task_conv)`):
  - each ref exists in **this** task's conversation;
  - for a tool call, its result message is not an error (`ToolResult.kind != error`);
  - for an attachment, it is bound in this task.
  - An item with no valid ref shows **"claimed, not verified"**, never ✅.
  - `done` requires every item verified. Otherwise the task is `partial` or `not_done`.
- `outward: true` items need a ref to a **non-read-only call that went through approval** (`ToolConfirmResolved approved=true` in `run_events`) and succeeded. "Sent to Y" without that is "not verified".
  - I don't parse the recipient text against the item. That would be string matching. The approval card already showed Arsen the exact recipient.
- Honest limit, stated on the card's legend: a ref proves a source was fetched and an action succeeded. It does not prove the content claim, e.g. "≥3 options under €180".
- No second model pass in v1. I agree that "add a judge if false done is observed" was weak. The ref check is the cheap structural replacement, and it is deterministic and testable.

### 3. Brief written from memory, and on a call — **accept with change**

- **Verbatim in code, not by the model**: TaskService stores `brief.asked` = the user messages of the origin run (`run.input_text` plus its steers, `role=user, name=None`) **as stored**, in the original language. The model cannot drop or rephrase them.
- `assumptions[]` becomes a field of `tasks.delegate` and is shown on the card.
- **On a call**: `tasks.note(title)` only. The verbatim is attached by code from the current voice turn and any earlier turns the model points at by message id (optional). The card and the call screen show the verbatim, so a misheard name is visible.
  - The **full brief is written by the task run itself as its first step**, after the call. The task run gets the verbatim plus the call's messages (voice runs with that `call_id`), and must call `task.brief(goal, context, constraints, dod, assumptions)` before any other tool. This is enforced in `_execute_one`: other tools are refused until the brief exists.
  - This adds no extra model call and no wait on the line. The card shows the brief once it is written.
  - For typed delegation the asking model still writes the brief, because it has the chat.
- **Origin access**: rather than `sessions.read`, which stays off for TASK runs and is session-scoped, add a read-only `task.origin(limit)` that returns the origin conversation's recent said messages. It is allowed even for incognito, because the task shares the origin's incognito flag.
- **Measure**: put in the issue's DoD the voice-turn latency of a "noted" turn against a normal turn (TTFT and total, from `ModelUsage`), from live calls.

### 4. GPU — **accept**

- Keep the whole-call hold and `max_running=1` (A §9 and §5).
- **Park between steps**: at the top of each AgentLoop step (the steer/cancel check, `loop.py:326-330` per 03-as-is), a TASK run waits while `engine.call_open()`.
  - It keeps its status `running` and shows "paused for your call" on the card.
  - It holds a run slot but no GPU. `max_running=1` bounds that to one slot.
  - An in-flight model call finishes. One prefill can still overlap the first voice turn; that is accepted and measured.
- **Routing**: the default stays the CHAT lane (A). The alpha measures CHAT vs BACKGROUND lane for a real research task (seconds per step) and voice TTFT. The default is set from those numbers.
- **DoD**: voice TTFT p50/p90 on the live box with the call idle vs one task running. The numbers go in the issue. D's stored-settings validator is adopted so `run_routing`/`budgets` actually contain TASK on live.

### 5. Call end and read-back — **accept with change**

- **Heartbeat**: `call.alive` every 20 s from the call session. The sweeper ends a call only when there has been **no heartbeat for `call_idle_s` (default 120 s, at least 4× the interval)** and no voice run is executing.
  - Silence with a live page never ends a call.
  - `call.ended` on hang-up and sendBeacon on pagehide remain.
  - All three paths use the same atomic claim.
- **Real-phone test is a DoD item**: Arsen's Android Chrome, screen locked and in the ear-pose black screen for 3+ minutes. Noted tasks must not start, and must start within `call_idle_s` after the tab is killed.
  - Headless can't prove this. The 03/memory rule "verify on the live app" applies.
  - Expectation, unverified: an active `getUserMedia` plus playing audio keeps Chrome from intensive timer throttling, but this must be measured, not assumed.
- **Read-back off the hang-up button**: accept. The hang-up button ends the call immediately.
  - The noted list on the call screen is the confirmation. It shows the verbatim with slots as heard, not an English title.
  - The voice-side `TasksBlock` tells the model: when he signals he is wrapping up and tasks are noted, read them back in one sentence with the key details (who/when/how much) as heard. "Wrapping up" is the model's judgement, not keyword detection. "What have you got?" is answered from the same block, with the same slots.
  - **Note**: this narrows Gate A's "before hang-up Jarvis says the list" from guaranteed to model-led. Arsen should confirm. A dropped call still gets the "Started N tasks from the call" push.

### 6. Voice-style migration — **accept, already verified**

- The live `voice.style` is quoted in the facts table above. It is Arsen-edited and contains the exact sentence.
  - A's upgrade (remove that exact sentence, keep his paragraph) **works on live**.
  - D's "equal to a previous default" **would be a no-op on live**, so I reject D's variant.
- In addition, TasksBlock says plainly that the note-for-later rule takes precedence over any "do it now" wording in the style, for the case where the sentence survives (an edited or older variant).
- If the upgrade does nothing, it logs a warning, and Settings → Voice shows "this style predates tasks".
- **Live check in the DoD**: a real call on the phone produces a "noted" task, and the task starts after hang-up.

### 7. "Can I" re-check at start, not only from model-declared needs — **accept with change**

- The CapabilityBlock ships in slice 1 with tasks (A §7). Browser extension connectivity is already a provider in `provider_health()`: `mark_unavailable` (`tools/registry.py:151-159`) sets `browser`'s error when Brave disconnects. It must be named per browser/host in the block, e.g. "browser@jarvisvm".
- **Re-check at start**: when a noted or queued task is claimed to start, TaskService checks every provider the brief's `needs[]` names. If one is down, the task becomes `not_done`: "not started: X is down since hh:mm". The card is pushed. Nothing runs.
- **Not relying on `needs[]` alone**: in a TASK run, the first tool call that fails with a `cannot_route` "not reachable" error (`registry.py:239-240`, a status signal, not text matching) ends the task `not_done`, with that reason as `stopped_reason`.
  - The engine does this through a typed flag on the ToolResult, `unreachable=True`, set where `cannot_route` produces it. It does not parse the message.
  - So an omitted `needs` entry still fails honestly and early, without a vague wrap-up.

### 8. Who may delegate; approval expiry — **accept with change** (one factual correction)

- Correction: `sessions.say` creates COLLAB runs (`sessions.py:341`), and COLLAB is already denied `tasks.*` in A §12. The real gap is that `_deliver` can **steer** text into a working CHAT run (`sessions.py:335-337`).
- **Owner origin in code**:
  - `Run.origin: Literal["owner","system"]`, set to `owner` only in `api/ws.py:100` (`run.create`) and `:119`. It is stored, so add it to migration 0017 next to `call_id`.
  - Delegation is allowed only when `run.origin == "owner"` **and** the run has received no steer from another session in this run.
  - `sessions._deliver` marks the RunControl as `foreign_steer=True` when it steers. Then `tasks.delegate/note` refuse with "a task can only start from Arsen's own message".
- **Exact arguments**: the card shows the tool's arguments (recipient, subject, first lines, URL, typed text, amount) from `ToolConfirmRequested.arguments` (`loop.py:966-970`), the same as ConfirmCard.
- **Expiry**: `settings.tasks.approval_ttl_s` (default 4 h). TaskService's sweeper resolves an expired wait as rejected, with the note "approval expired — re-read the source (thread/page) and ask again if still needed" (via `engine.confirm(..., approved=False, note=...)`). The model then re-reads, and a fresh card with fresh arguments appears.
- **Push opens the card, no one-tap approve**: the waiting push becomes `type:"notice"` with `path=/c/<origin>#task-<id>` (sw.js `:49-67` notice path), not the call-ring with Answer/Decline. Done and failed reports keep the ring (open question 4).

### Smaller items

- M1/M2: A's report placement (wait for the origin run) and the journal skip stay. **Agree.**
- `tasks.enabled=false` until the alpha passes. **Agree** (A §13).
- An alpha with no MCP cannot exercise the gates. **Agree.** The alpha includes:
  - one real research task with real read-only tools and the browser (judged on quality);
  - one task that reaches `browser.type`/an outlook draft, to exercise the approval card end to end on the :9121 alpha core.
  - Never on prod mail.
- Anchor message: it stays `role=user, name="task"`, the same convention as `supervisor`/`ledger`/`playbook`. Its content starts `[system note — not from Arsen]`, and SYSTEM_RULES gets one line saying that named bracketed notes are machine notes. A test checks that `earlier_turn_view` keeps the name and that the web doesn't render it as Arsen's bubble.

## Net design changes to architect.md

1. Strict policy in TASK runs: non-read-only asks unless on `tasks.no_approval`; `tasks.deny_tools` hides shell/fs writes.
2. DoD refs verified in code; "claimed, not verified"; outward items need an approved, successful call.
3. `brief.asked` verbatim captured by code; `assumptions[]`; on calls `tasks.note(title)`, with the brief written by the task's first step via `task.brief`; `task.origin` read tool.
4. Park TASK runs between steps while a call is open; lane decided by alpha measurement; TTFT p50/p90 in the DoD; D's stored-settings default validator.
5. Heartbeat-driven call end (120 s, ≥4× interval); the hang-up button is plain; model-led read-back with slots; a real-phone locked-screen test.
6. Exact-sentence voice-style upgrade (verified to apply on live), plus a TasksBlock precedence line, a warning and a hint.
7. Health re-check at task start; a typed `unreachable` on ToolResult ends the task `not_done` early.
8. `Run.origin` plus a foreign-steer flag gate delegation; approval TTL with forced re-read; a notice push to the card.
