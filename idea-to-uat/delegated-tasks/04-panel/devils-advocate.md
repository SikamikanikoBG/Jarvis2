# 04 — Panel: devil's advocate (2026-10-29)

Inputs: 01-needs.md, 02-mockup/DECISION.md + mockup.html, 03-as-is.md, docs/journal.md,
docs/stories/12_assistant.md, and the code at main (claims below checked against the code).

**Bottom line.** The idea is right: the pain (silent calls, steers piling into one long run,
results scattered over chats) is real. The obvious build, a model that triages every ask,
writes briefs, spawns sub-agents that act unattended and reports "done", piles three known
failure modes onto one 27B on a shared GPU. Those modes are unattended outward actions,
self-reported success, and judgement the model can't reliably make. Build a narrow first
slice whose safety and honesty are enforced in code, then add the voice "noted" flow.

---

## Critical

### C1. The confirmation gate does not cover what sub-agents will actually do
`needs_confirmation` (proto/settings.py:461) fires only for tools that say `destructive`.
For MCP tools that is only an explicit `destructiveHint=True` (tools/mcp_provider.py:159).
The browser extension marks **every** tool `destructive: false`, including `browser.click`,
`browser.type` (`submit=true`) and the page-script tool (extension/background.js:78-211). That
extension runs in Arsen's **real Brave profile with real logins** (extension/README.md). The
mockup's own example task ("Hotels near Rathaus … step 6 · booking.com search") is a sub-agent
clicking around booking.com while logged in as Arsen. A misread brief ("book the 2nd one")
gets to a paid booking, a sent LinkedIn/WhatsApp message or a submitted form with **no pause**,
even in an attended run. US-7 lists "send mail, delete, reboot, pay, message a person". Today's
gate stops only the first of those reliably (outlook_send, plus shell_run/fs_write/calendar_*).
`always_allow` (e.g. "workocholic.shell_run", as the settings docstring suggests) is honoured
in task runs too.
Also, `_UNATTENDED` (engine/loop.py:94) makes SCHEDULED/SYSTEM runs skip every confirmation,
including `always_ask`. If task runs reuse either kind, or the "nobody is present" scheduled
context, we rebuild the 2026-10-15 reboot path.
**What would make me accept it:**
- Task runs are a kind of their own, **never** in `_UNATTENDED`, and get a test that fails if
  someone adds them there.
- Inside a task run, `always_allow` is ignored. Every non-read-only call to a tool with
  `openWorldHint`, and every non-read-only `browser.*` call, pauses unless it is read-only.
  The test is "is this tool read-only", with no list of words or tool names: that is the MCP
  annotation the code already reads. This needs a small change to the extension's
  annotations: browser.click/type/eval are `openWorld`.
- Task runs can't touch Jarvis's own deployment. Use no `shell_run` in task runs at all in
  slice 1, a per-kind tool exposure setting rather than a pattern on command text. The
  2026-10-11 outage was exactly "Jarvis did a long job unattended on jarvisvm and
  deployed itself".

### C2. "Done" is self-reported, and nothing checks it
The planner's `plan_step_done` is the model ticking its own box. `_wrap_up` (loop.py:1119) is
the same model summarising itself. A 27B under budget pressure reliably writes "✅ sent",
"✅ 4 options under €180" when a tool errored, a page didn't load, or the price included
taxes it never saw. That is the "it's fixed" pattern Arsen has been burned by, automated and
sent to his phone.
**Acceptance:**
- Each DoD item on the card has a state of `done` only with **evidence**: a reference to a
  successful tool-call id in that task's ledger (the code checks that it exists and is not
  `isError`), an attached file, or a URL that was fetched. A model can still cite the wrong
  evidence, but it can't cite one that doesn't exist.
- If the evidence check fails, the item is shown as **"claimed, not verified"**, never ✅.
- Outward actions are checked mechanically: "sent X to Y" counts as done only if an
  `outlook_send` to Y succeeded in the ledger.
- A second model pass ("does this evidence prove this item?") is optional and comes later. Do not
  depend on it: it is the same 27B grading its own work.

### C3. A brief misread by a context-less sub-agent, acting on Arsen's behalf
The sub-agent works from a brief the 27B wrote, not from the chat (US-2). Compression drops
exactly what matters: "+1 person", "not Hilton", which Gyuzelev mail, "3 nights not 2", a
Bulgarian name the STT got wrong. A human assistant who misread a brief comes back and asks;
this one reads its own brief as true.
**Acceptance:**
- The brief always holds Arsen's **verbatim** ask(s) (the transcript text, original language)
  next to the model's paraphrase.
- It also has an explicit **"assumptions I made"** list, shown on the card and read in the
  after-call read-back.
- The sub-agent may `sessions.read` the asking chat (read-only). It is told to stop with
  "needs input" when the brief and the source disagree.
- Recipients and amounts in a confirmation come from the tool args, shown verbatim, never from
  the brief's prose.

---

## High

### H1. GPU contention: delegating more doesn't make anything faster, it makes voice slower
There is one 27B. Prefill is ~1,200 tok/s at the hardware ceiling (journal_ttft.md), with 2 calls
per endpoint and 3 runs in all. A research sub-agent quickly holds 60–100k tokens of context
(the newsletter: 650k tokens over the run). Story 12 already measured that a re-read prefix
costs a voice turn 21 s instead of under 1 s. `max_concurrent_runs_per_endpoint=2` exists
*because* long runs evict each other's cached prefixes. Each task run is another long run, in
its own conversation, so it has its own prefix to evict.
- If tasks go on the **chat lane**, they compete with voice. `_BESIDE_A_CALL` only gates
  *starting* while a voice run executes, and background runs start between call turns
  (run_engine.py:52-54).
- If they go on the **background lane**, the newsletter showed 4 minutes a step. A "few
  minutes" task becomes 40, or competes with triage.
- After a call, three noted tasks start at once and take all three run slots. The 08:30
  schedule then queues, and so does Arsen's next call or typed question.

Delegation doesn't add capacity. It moves waiting from "on the line" to "in the queue", which
is fine, but only if the queue is honest and voice never pays for it.
**Acceptance:**
- A setting for **concurrent task runs, default 1**. The rest queue, and the card shows
  "queued, 2nd" (EC-6).
- Task runs never use the voice +1 slot. A task step does not start while a voice run executes
  (add the task kind to the call gating; don't leave it CHAT).
- One budget per task kind, sized between CHAT (600 s) and SCHEDULED (3600 s). It is set in
  settings, not inherited by accident.
- Measured before/after: voice TTFT p50/p90 on a call while one task runs. This goes in the DoD
  with numbers from the live box.

### H2. The honesty check asks the 27B to know what it can't know
US-5: "can / knows enough / understands the goal". The mockup's own example is wrong. "Book a
table … I have no booking access" is false: Jarvis has a logged-in browser and could book. So
the model will misjudge in both directions. It will refuse things it can do and take things it
can't (a host that dropped 30 s ago, a site behind a login that expired). LLMs systematically
over-claim "I have enough to go on". "Can" is a fact, not a judgement.
**Acceptance:**
- **"Can"** is computed by code from `provider_health()` / `cannot_route()` / the live tool
  registry and handed to the model as a short capability block. It is re-checked when the task
  *starts* (after a call, maybe 20 minutes later), not only when the ask is made.
- **"Knows enough"** and **"understands"** stay model judgement, but they show up as the
  assumptions list (C3). Do not build them as a gate or classifier.
- No "decline" without naming the concrete missing tool or host. A decline Arsen thinks is
  wrong has a one-tap "do it anyway".
- No keyword rules for "is this long", anywhere: the model calls `tasks.delegate` or it doesn't.

### H3. Voice: noting tasks costs call latency, and STT errors become tasks
- Each "noted: X" is a tool call. On a think-off voice turn that is a second model step, about
  1–2 s more silence per note. If the model writes the full brief *on the call*, it is worse:
  a long tool-call argument is generated while Arsen waits.
- Whisper on Bulgarian names, dates and amounts (journal 2026-09-16: Greek letters for a
  short utterance). "Reply to Гюзелев" → wrong person; "14–16" → "4–6". If the read-back is
  in English (Arsen gets English), a translated title can hide the misheard slot.
- "Before you go" read-back assumes Jarvis knows the call is ending. Arsen often just presses
  the red button, or the phone kills the tab. Hang-up isn't signalled to core today, and drop
  and hang-up look the same (as-is §4). Detecting "bye" is a keyword heuristic in waiting.
- `VOICE_STYLE_DEFAULT` says "never 'after the call'", and the stored copy overrides a
  changed default (as-is §4.5). If that is shipped without a migration, the live app behaves
  the old way while the alpha passes.

**Acceptance:**
- `tasks.note` on a call stores **only** the verbatim utterance(s), a short title and the
  conversation pointer. It is persisted server-side at note time (not in the browser store),
  with no brief writing and no thinking. The brief is written after the call, from the call
  transcript, off the voice path.
- The read-back speaks the critical slots (who, when, how much) as heard. Asking "what have
  you got?" is how Arsen gets it, and it is not required before ending.
- Starting: a `call.end` client message, **or** socket closed plus no reconnect within a grace
  period (a setting, e.g. 90 s). The start is claimed with a UNIQUE row (the `schedule_fires`
  pattern), so it happens exactly once.
- If the call reconnects within the grace period, the noted list is still editable.
- The voice-style change ships as a migration of the stored value or as a separate block.
  It is verified on the deployed settings, not the default.

### H4. Approval from a push equals approval without reading
A task that pauses for each of 6 browser submits sends 6 pushes. Arsen approves from the lock
screen while driving. Two outcomes are likely: approvals become reflexive, so the gate is
bypassed in practice, or the task sits "waiting on you" for hours. Then it resumes on stale
context: the mail thread got two new replies, the hotel price changed. A cold resume also
re-prefills the whole context (H1).
**Acceptance:**
- An approval shows the exact action from the tool args (recipient, subject, first lines,
  amount, URL).
- It expires (a setting). Resuming after expiry re-reads the source (thread, page) before the
  action is offered again.
- One task produces at most one pending approval push at a time.
- No approve from the notification itself; it opens the card.

### H5. Restarts and redeploys during tasks are routine, not an edge case
The daily review cron (05:15) can deploy fixes. Arsen and other Claude sessions deploy several
times a day. `_resume_on_boot` re-enqueues, and a resumed run with an unanswered mutating call
asks "did it complete?". In a task, nobody is there to answer, so the task goes to waiting for
Arsen with a confusing question. In-memory chains and voice asks are lost (as-is §7).
**Acceptance:**
- Tasks, noted asks and task↔run links all live in the DB (0017), with nothing that matters
  held in memory.
- Interrupted tasks show **"interrupted by a restart at 05:17, resumed"** on the card, or "not
  resumed: an outward step may have run; check X" for unconfirmed mutating calls.
- An alpha test kills core mid-task (and mid-call with noted tasks) and checks exactly-once.

---

## Medium

### M1. Reports landing in a live chat: serialization and cache churn
No per-conversation serialization (as-is constraint 3). A report written into the asking chat
as a *message* while Arsen has a run going there interleaves with that run's history. It
changes the prefix of the next turn, and the web transcript gets an insert mid-stream. That is
the same class of bug as the 2026-10-06 reply-comments repaint that passed every mock.
**Acceptance:**
- The report is a task entity the web renders (card), not a Message in the run history.
- The model learns about it as one compact line in the next turn's context.
- Live-app check in Brave **while a reply is streaming** in that chat when a report lands.

### M2. Push and journal noise
`jarvis.call_me`: "Chats and scheduled runs ring by default" (tools/builtin.py:203). Add a ring
per task, an after-call report, and waiting-on-you pushes, and 4 asks become 6+ rings. Each task
conversation also gets a day-journal digest, so the overview (TodayBlock, carried on **every
call turn**) changes each time a task ends. That defeats story 12's "unchanged block is sent as
a reference" and re-sends the block on the voice path.
**Acceptance:**
- One ring per asking chat per gather window. The after-call batch gets **one** ring.
- Task conversations are excluded from `overview()`/`sessions.list`. Their outcome is folded
  into the asking chat's digest.
- No ring while Arsen is on a call (EC-8): the push waits until the call ends.

### M3. A new RunKind touches ten maps; the MAIL kind already shows the miss
`create_run(kind=MAIL)` would KeyError today. A TASK kind must be in `_PRIORITY`, routing,
budgets, ConversationKind, call gating, `_UNATTENDED` (deliberately *not*), `_CONTEXT_KINDS`,
callme defaults, journal, web types (hand-mirrored, 1,228 lines) and the reducer.
**Acceptance:** one parametrised test over `RunKind` that every map covers every kind, with
MAIL fixed in the same change.

### M4. "Only announced tasks" cannot be enforced by reading the model's text
The DECISION says self-talk is allowed only for tasks Jarvis announced. Code can't verify that
the model *said* it.
**Acceptance:** the announcement **is** the card. `tasks.delegate` creates the visible card
(and emits the event) before the sub-run is queued. Task runs don't have `tasks.delegate` or
`sessions.say`, so the depth is 1 by construction (EC-17) instead of a hop counter.

### M5. Incognito, language, recipients
- Incognito must propagate to the task conversation: journal, KG, sessions, shadow recorder
  and digests (EC-9). Add a test for each sink.
- The brief carries the output language for third parties (Bulgarian mail stays Bulgarian).
  The report to Arsen is in English.

---

## Low

- **EC-2 duplicates:** catch double-sends with client_ref/utterance idempotency. For semantic
  duplicates ("check Y" said twice), let the model see open tasks in context and ask. Do not
  build a similarity threshold, which would be a heuristic filter.
- **EC-14 steering a running task:** ambiguous with 2+ tasks, and it is rare. Slice 1 does
  cancel + re-ask. Steering comes later.
- **EC-13 never-ending task:** `_wrap_up` already covers it. Make sure the card says
  "stopped at budget".
- **Browser work tabs:** the 3-minute close grace is per conversation. A task conversation
  needs its own tab, and Brave on jarvisvm is down after a reboot until ArDi logs in. "Can"
  (H2) must include "browser connected".
- **Sidebar clutter:** hidden task conversations must stay out of the chat list. 259 of them
  are already mostly scheduled-run chats.

---

## Simpler alternatives and a smaller first slice

Much of the plumbing exists. A run with `call_me` already rings on done/failed/waiting
(features/callme.py). Runs already resume, cancel and confirm. Today the chat "blocks" because
typed messages during a run become **steers** into it (web store.ts:720). Ending the
orchestrator's turn fast removes that pain by itself.

**Slice 1, "background it" (most of the typed-chat value, about a third of the risk):**
1. `tasks.delegate(title, verbatim_ask, goal, dod[], assumptions[])` creates the card, then one
   run in a hidden linked conversation (new TASK kind, 1 concurrent, attended confirmations
   with C1's openWorld rule, no shell, no delegate/say tools).
2. The card in the asking chat shows state, brief, Cancel, Approve/Deny and the report with
   **evidence-checked** DoD items (C2). There is one push on finish via the existing callme
   path.
3. "What are you working on?" uses `tasks.list`. The capability block from code (H2).

**Slice 2, the call:** server-side `tasks.note`, call.end plus a grace period, an exactly-once
start, briefs written after the call, the after-call card, and the voice-style migration.

**Slice 3:** steering, an optional verifier pass, the cross-chat tasks view (mockup option B),
and smarter duplicates.

**Don't build** (yet): the model as a take/ask/decline *gate*, semantic duplicate detection,
nested delegation, starting tasks *during* a call, or a "Jarvis decides by itself to delegate
without a card" path.

## The DoD for this feature itself (to avoid another "it's fixed")
- Live, on Arsen's Android + Chrome: a real call with 2 noted asks (one Bulgarian name), hang
  up with the red button, get the after-call card, and the push rings (headless can't prove
  push).
- In live Brave: a report lands while a reply is streaming in the same chat, with no repaint
  glitches.
- Measured voice TTFT with one task running vs idle, with numbers in the issue.
- Fault injection on the alpha (:9121): kill core mid-task and mid-call; take jarvisvm's host
  offline before the start; a browser submit inside a task must pause.
- A deliberately failing task (a site that 404s) must report ❌ or "not verified", never ✅.
