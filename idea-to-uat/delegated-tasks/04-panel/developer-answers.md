# 04 — Developer: answers to the challenge round

I checked the claims in the code and, read-only, on the live box (`ssh ardi … docker exec jarvis2-core`, a
`mode=ro` read of `/data/jarvis2.db`). Three facts from that change the plan more than any argument did:

- **Live `confirmations` = `{"mode": "off", "always_allow": [], "always_ask": ["*.outlook_send"]}`.** In production,
  nothing asks except Outlook send. My line "confirmations already pause" (developer.md slice 1) is **false
  on live**. A task under my plan would have acted with no pause at all.
- **Live `voice.style` was edited by Arsen on 2026-10-20.** He added English-only and spoken-numbers rules. It still
  contains the exact sentence `You have all your tools on the call: do what he asks now, never "after the call".`
  My "swap only when it equals an old default" would therefore be a no-op on live. The rebuttal is right.
- **Live `budgets` is stored** (chat 250 steps, etc.) and has no TASK entry. This confirms that the
  "kinds added later get defaults" validator is needed (it was already in slice 1).

Other code facts:
- Every browser tool in `extension/background.js` (L63-228) is `destructive: false`.
- `mcp_provider.py:158-159` sets `destructive` only on an explicit `destructiveHint: True`. Every MCP tool without
  annotations (most of them, including the hosts' `shell_run`) is therefore non-destructive. The hole is wider
  than the browser.
- `ToolSpec` (`proto/tools.py:10-26`) has no open-world field.
- `sessions.say` creates **COLLAB** runs (`sessions.py:341`), not CHAT. The same holds for collab MCP
  (`collab.py:95`) and the OpenAI-compatible API (`openai_compat.py:73`). Only ws `run.create` (`ws.py:100`, `:119`)
  makes CHAT runs.

---

## 1. Browser / outward actions not gated: **ACCEPT WITH CHANGE**

Accepted in full: A's strict rule (A:226-228) instead of mine. TASK runs ignore `mode=off` and `always_allow`.
This is no longer optional, because live is `mode=off`.

Change: I gate on **"not read-only"**, not on a new `openWorldHint`.
- An `openWorld` flag needs a proto field, `mcp_provider` mapping and an extension release, and it still misses
  every MCP tool that sends no annotations (the norm, see above).
- The rule for TASK runs: **every non-read-only call pauses for approval, unless its name matches
  `settings.tasks.auto_allow`**. That list is settings data, not a keyword filter. Default: the core's own
  internal writes that cannot leave the house: `files.attach`, `task.*`, `notes.*`, `kg.*`, `jarvis.*` plan
  tools.
- This catches `browser.click/type/eval`, `*.shell_run`, `*.fs_write`, `mail.send` and everything else, with no
  extension change.
- Implementation: `needs_confirmation(..., strict=run.kind is TASK)`, which returns `not read_only and not
  auto_allowed` (`settings.py:463-470`); the call sites are `loop.py:713`, `:810`, `:821`.
- `_can_parallelise` already rejects anything that needs confirmation.

Also accepted: `settings.tasks.exclude_tools`, default `["*.shell_run", "*.fs_write", "*.fs_delete"]`. These
tools are not offered to TASK runs, and `_execute_one` refuses them by name (the same as `loop.py:760-765`).

Cost: a research task that clicks through a site asks for approval on every click. That is right for v1. Arsen
can add `browser.*` to `tasks.auto_allow` himself, with the risk spelled out in the settings description.

Tests:
- A TASK run doing `browser.type(submit=true)` (fake browser provider, `read_only=false`) ends `waiting_user`
  with `mode="off"` set.
- The same with `always_allow=["browser.*"]`: it still waits.
- `shell_run` is not in TASK's exposed tools, and naming it is refused.
- CHAT behaviour under `mode=off` is unchanged (regression).

Effort: +0.5 d in slice 1 (moved from slice 4).

## 2. Evidence nobody checks: **ACCEPT WITH CHANGE**

Accepted: each DoD item's `evidence` is a reference, `{call_id}` or `{attachment_id}`, to something from this
task's own ledger. TaskManager verifies it:
- the tool message with that `tool_call_id` exists in the task's work conversation (any of its runs);
- its result is not an error;
- for an attachment, it is bound to this task's run.

If the reference fails, the item is shown as **"claimed, not verified"**. That is never ✅, and the task cannot
be `done`; at best it is `partial`. A URL counts only when a successful `fetch`/`browser` call in the ledger
carries it.

Change on "sent to Y": I will not match recipients against the item's free text in code, because that is text
parsing by another name. Instead, a DoD item may be declared `outward: true` when the brief is written. Such an
item verifies only against a cited call that is **non-read-only**, succeeded, **was approved by Arsen** (a
`tool.confirm_resolved approved=true` event for that call id, which strict mode guarantees exists) and was
**not rewritten into a draft** (the `[saved as a draft: …]` policy note, `loop.py:866-868`). Whether the
recipient is the right person is what he saw on the approval card. That is the real check, and he is the one
making it.

Tests: a fake worker that claims done with a non-existent call id → partial with "claimed, not verified"; with
an errored call → the same; with a valid read call → ✅; an outward item citing a call that became a draft →
not verified.

Effort: +1 d in slice 1.

## 3. Brief from memory, and written on a call: **ACCEPT WITH CHANGE**

Accepted:
- `brief.verbatim: list[str]`: the user messages the task came from, in the original language, copied **by
  code** from the asking run's own user messages (and steers). The model does not retype them; the tool takes
  message ids or defaults to the current turn's.
- `brief.assumptions: list[str]`, shown on the card.
- On a call, `tasks.note` takes only `{title, verbatim_hint?}`. The voice turn stays a one-sentence "noted",
  and the verbatim text is captured from the run's messages by code.
- Measure the latency of a noted voice turn against a normal one on the alpha with the real model, and put the
  numbers in the issue.

Change: "the full brief is written after the call, before the run starts" is done by the **worker's first
step**, not by a separate summariser pass.
- The worker's first message holds the verbatim lines plus the call's transcript: the messages of the runs with
  that `call_id`, copied by code into the task conversation.
- The worker's first required tool is `task.brief(goal, context, constraints, dod, assumptions)`. The card
  updates from it, and the work starts after it.
- This adds no extra model round trip, and it is written by the model that will do the work.

Change: no `sessions.read` of the origin. Sessions excludes incognito chats, and a worker reading the live
parent mid-conversation brings in noise. Copying the relevant messages by code is deterministic and keeps
incognito handling. Typed delegation keeps the asker-written brief (it has the whole chat), plus verbatim and
assumptions.

Tests: the verbatim text equals the stored user message bytes (Bulgarian kept); a noted task starts with the
call transcript in its first message; a worker that skips `task.brief` is nudged once (the open-plan pattern,
`loop.py:517-540`), and after that the card shows "brief not confirmed".

Effort: +0.5 d slice 1, +0.5 d slice 2.

## 4. GPU: **ACCEPT**

- Whole-call hold (A:240): `on_a_call()` also counts an open call from the call ledger, not only a voice run
  executing (`run_engine.py:316-321`).
- `max_running=1`.
- Lane: default **CHAT**, like A and like SCHEDULED (my BACKGROUND choice ignored the 2026-09-17 "four minutes a
  step" finding written in the settings comments). Then measure both on the alpha and set the default from the
  numbers.
- **Running tasks park between steps while a call is open.** At the top of the loop step, next to the
  cancel/steer check (`loop.py:317-330`), a TASK run awaits `engine.call_clear` (an `asyncio.Event`), racing
  `ctl.cancel` so that cancel still works. The parked time is credited through `watch.paused_s`, the same as a
  wait tool (`loop.py:872-873`), so the budget measures work. One in-flight model step can still finish, which is
  at most one step's prefill. That is accepted and measured.
- DoD item: voice TTFT p50/p90 on the live box, with a call idle and with a call while one task is parked.

Tests: a TASK run queued during an open call does not start; one running parks at the next step and resumes at
call end; cancel while parked works.

Effort: +0.5 d slice 2.

## 5. Call end and read-back: **ACCEPT**

- The heartbeat (`call.alive`) drives the sweeper.
- Chrome's intensive throttling limits hidden-page timers to one wake-up a minute. So `call_alive_s=30` and
  `call_idle_end_s=240` (at least 3 throttled intervals plus slack), both in settings. Each voice
  `run.create`/`steer` also counts as alive. I also add a `visibilitychange`/`pagehide` beacon for a real close.
- A real-phone test is a **DoD item**: Arsen's Android, a call with the screen locked (and in the ear-pose black
  screen) for 4 minutes, and no `dropped` end may occur. Headless proves the logic, not the throttling.
- No read-back on the hang-up button. I drop A's first-press behaviour; red means hang up. The live `NotedList`
  on the call screen is the confirmation, and the model reads back when he asks or when he signals he is
  wrapping up.
- The read-back carries the slots: `TasksBlock` lists each noted task with its **verbatim** words (challenge 3),
  and the instruction is to read back who / when / how much as heard. It is not code-extracted slots, which
  would be parsing.

Effort: +0.5 d slice 2 for the real-phone check.

## 6. Voice-style migration: **ACCEPT** (verified, see the top of this file)

Verified: the live style was edited, so my swap does nothing. A's "remove the exact sentence" would work today,
because the sentence is present verbatim.

Plan:
1. The work policy for calls moves into `TasksBlock` on voice runs. The block says explicitly that it **takes
   precedence over any "do it now / never after the call" line in the voice style**. It is per-turn and appears
   only when `tasks.enabled`.
2. A settings upgrade removes that **one exact sentence** from the stored `voice.style` when present, and keeps
   Arsen's additions. The sentence is a versioned constant of our own old default, not a word list. It is not
   removed from `VOICE_STYLE_DEFAULT` until then (the new default drops it).
3. If the sentence is not found and the style still differs from the default, log a warning, and Settings → Voice
   shows "your call style may conflict with task noting".
4. The live check is a real call on production that produces a noted task, which then starts after hang-up.

Test: the live string quoted above as a fixture → after the upgrade, the sentence is gone and the English-only
and numbers rules are intact.

## 7. "Can I" re-checked at start, not dependent on `needs[]`: **ACCEPT**

- The health block (A's `CapabilityBlock`) moves to **slice 1**.
- It is re-checked at `start()`, including when a noted task starts after the call. An unreachable provider among
  `needs[]` gives the card "not started: X is down since hh:mm"; nothing runs, and it is not a silent failure.
- The browser provider already reports `mark_unavailable` errors (`registry.py:153-160`, `ws.py:187-190`), so the
  extension counts as a provider with no extra work. The card names the browser by its connection name.
- Not relying on the model listing its needs: a TASK run whose tool call comes back as "provider not reachable"
  ends the task as `not_done` with that reason and the time.
  - The run stops at once, without `_wrap_up` vagueness.
  - The check uses the structured state the error came from (`registry._errors[namespace]` at the moment of
    `validate()`/`cannot_route()`), not the error text.
  - This is a TASK-only branch in `_execute_one` next to `loop.py:752`.

Tests: noted task + provider down at call end → not started with the reason; provider down mid-run → `not_done`
with the reason, no wrap-up; `needs[]` empty but the browser disconnected → the first browser call ends it
honestly.

Effort: slice 3 shrinks to ~0.5 d (only the decline wording polish); +1 d moves into slice 1.

## 8. Who may delegate; approvals hours later: **ACCEPT WITH CHANGE**

The premise is partly wrong in the code: `sessions.say` makes COLLAB runs, not CHAT (`sessions.py:341`).
Scheduled runs are SCHEDULED, and collab and the OpenAI-compatible API are COLLAB.
- "Only CHAT runs may delegate" therefore already means "only runs started by Arsen's own ws `run.create`".
- I make that explicit and checked in code: `Run.origin = "owner"` is set only in `ws.py:100/119`, and
  `tasks.delegate/note` require it, not just the kind. The ws accepts a client-chosen `kind`, so the kind alone
  is a weaker guarantee.
- One real gap remains: `sessions.say` can **steer** into Arsen's running CHAT run (`sessions.py:335-336`). I
  mark such steers (`engine.steer(..., source="session")` → `RunControl.steered_by_session`), and a run that
  received one cannot delegate for the rest of that run.

Approvals:
- The card already shows the exact arguments (`ConfirmCard` renders `prettyJson(args)`, `ConfirmCard.tsx:34`).
  The push body adds the tool name and the main argument values as data.
- Expiry: `settings.tasks.approval_ttl_s` (default 4 h). On expiry, TaskManager resolves the confirm as
  **denied** with note `expired — re-read the source before asking again`. The worker resumes, its system block
  tells it to re-read the thread or page and ask again, and that produces a fresh card with current content.
- Approve is never a one-tap action in the notification: the push opens the card (`sw.js` has no notification
  actions, and none will be added).

Tests: a session-steered CHAT run cannot delegate; a CHAT run without `origin=owner` (made through
`create_run` directly) cannot delegate; expiry → denied + re-ask path.

Effort: +0.5 d slice 1 (origin), +0.5 d slice 4 (expiry).

## Smaller items

- **Report placement: I now agree with A.** The report is posted only when the origin conversation has no
  working run, waiting on `on_finished` (which becomes a hook list). On a call, `TasksBlock` says "finished
  since your last turn" and the model mentions it at a pause.
  - The anchor (A:187) needs care. `context.py` uses `name` only for bookkeeping, and I have not verified that
    the vLLM chat template shows `name` to the model at all, so we must not rely on it. The anchor text must
    therefore say so itself: `[core note, not Arsen] Task "<title>" finished; the report follows.`
  - Test: `last_turn_skills` (`context.py:267`) ignores it (it takes `name is None`), and the transcript renders
    it as the card position, not as a bubble.
- **Journal: I now agree with A.** `_SKIPPED_KINDS += TASK`, and no unread flag for TASK conversations. The
  report in the parent is what the journal and unread should reflect, through a `journal.touch(parent)` after the
  report is written. The waiting state still reaches "what's waiting on me" through `TasksBlock`/`tasks.list`
  and the card.
- **Whole-call hold and strict confirmations: I agree with A** (challenges 1 and 4). I keep the heartbeat (mine)
  over A's silence-only sweeper.
- **`tasks.enabled=false` until the alpha passes: accept.** The default is off, and Arsen switches it on in
  Settings after UAT. The tools and blocks are absent when it is off, so live behaviour is byte-identical before
  that.
- **Alpha with real tools: accept.**
  - The alpha gets read-only real providers: `fetch`, SearXNG `web.search`, `homelab` (read-only MCP).
  - One real research task is judged on quality: the brief, the verbatim, whether the DoD is verified.
  - The approval gate is exercised with a harmless non-read-only tool (`schedule.create`, `notes.*` minus
    auto_allow in the test settings) under `mode="off"`, to prove strict mode on the live-shaped settings.
  - No browser on alpha (it would drive Arsen's real Brave). Browser gating is proven in pytest with a fake
    provider and once on live with a task that only reads.

## Revised slices and effort

| Slice | Was | Now | What moved |
|---|---|---|---|
| 1 Typed delegation + card + verified report + strict gate + health | 5.5 d | **~9 d** | strict confirmations + exclusions (+0.5), evidence verifier (+1), verbatim/assumptions (+0.5), health + start re-check + provider-down end (+1), origin check (+0.5), deferred report placement + anchor (+0.5), the `enabled` flag |
| 2 Call noting + lifecycle | 5.5 d | **~7 d** | call hold + parking between steps (+0.5), brief-at-start from call transcript (+0.5), style sentence upgrade + warning (≈0), real-phone throttling check (+0.5) |
| 3 Honesty polish | 2 d | **~0.5 d** | most of it moved into slice 1 |
| 4 Approvals | 1.5 d | **~1.5 d** | strict gate moved to S1; expiry added (+0.5) |
| **Total** | ~14.5 d | **~18 d** | |

Slice 1 is bigger, but it is now safe to switch on alone with live `mode=off`, which it was not before.
