# 13 — "On it, I'll come back to you"

Every request to Jarvis was a run Arsen watched until it ended. On a call he sat on the line
while Jarvis worked, or hung up and lost the thread; typing, one long run blocked the chat until
it was done. A real assistant does neither. It takes the job, says so in a sentence, hands it to
someone with the context and a clear idea of what "done" means, and comes back when it is done -
or says plainly that it is not, and why. On a call it jots down what was asked, works through it
after hanging up, and then says what got done and what did not.

This story makes Jarvis work that way, without letting work nobody watches do anything to anyone
without Arsen (the 2026-10-15 self-reboot was a run nobody watched).

Built 2026-10-29 behind `settings.tasks.enabled`, off until the alpha passes (issue #10; needs,
mockup, panel and plan in `idea-to-uat/delegated-tasks/`).

## Stories

1. **He delegates instead of making me wait.** Longer work - research across sites, a document,
   a comparison - goes to a task, and the chat says so in one line and is free again. A quick
   lookup or a short answer he still does himself. Which is which is his judgement, not a rule
   on words.
2. **The task gets a brief and a Definition of Done.** Goal, the context from the chat (names,
   dates, links, files), constraints, a checkable DoD, what will need my approval, what he
   assumed. My own words are kept beside it, verbatim, so a paraphrase cannot drift from what I
   said. I can open the brief on the card.
3. **He reports back where I asked.** When it ends, the report lands in the chat that asked and
   one quiet notice opens it: per DoD item done, not done, or "claimed, not verified", the result,
   the files it made. Never "done" for something not done.
4. **On a call he notes, and works after I hang up.** "Noted" in a few words, the call stays a
   conversation; the call screen shows the noted list in my words as he heard them; he reads it
   back when I wrap up. At hang-up - or when the phone drops - the noted tasks start, exactly
   once, and one after-call report says how each went.
5. **He is honest about what he can take.** He knows what is down right now and says so up front
   ("can't now, the laptop host is down since 08:10"); a fact only I know, he asks for; a task
   whose machine goes down half-way says so at once instead of wrapping up around the hole.
6. **I can see and steer what is running.** A card per task in the chat, a `Tasks · N` chip in the
   header, "what are you working on?" answered from what is open. Cancel, a change of mind, an
   answer to its question - all from the card.
7. **Nothing outward without me.** Every action of a task that is not read-only - send, post,
   book, type into someone's site - stops for my approval, whatever my chat's confirmation
   settings say. The push opens the card; there is no one-tap approve.

## Design

### A task is a run of its own, in a conversation of its own — `features/tasks.py`

- `RunKind.TASK` / `ConversationKind.TASK`, wired like every other kind (priority 2: ahead of
  what the clock started, never ahead of him; the chat lane; 60 steps / 30 min). A test walks
  every `RunKind` for a priority, a lane and a budget - MAIL had none.
- Not a run inside the chat that asked: the engine does not serialise runs per conversation, so it
  would interleave with his next turn and churn that chat's cached prefix. The task's own
  conversation is hidden from the sidebar, never unread, skipped by the day journal, and reached
  from the card ("Open work").
- The `tasks` table (migration 0019) ties the chat that asked, the task's conversation and the run
  working in it. `TaskService` keeps the row in step with the run's events on the bus, the way
  CallMe does, and publishes `task.changed` to every client.

### Who may delegate

Only a chat run his own message started (`Run.from_owner`, set by the WebSocket's `run.create`
alone), and not one another session has steered into (`sessions.say` marks the run). A task
cannot delegate further. Hidden from the tool list and refused when named anyway. The 2026-09-19
rule - Jarvis does not strike up conversations with itself - holds for everything else.

### Nothing outward without him

A task is not "unattended" the way a schedule is - a schedule's prompt authorised its run; nobody
authorised a task's next click. In a task every non-read-only call waits for his approval
regardless of `confirmations.mode` and `always_allow` (live is `mode: off`), except the names on
`tasks.no_approval` (moving around a page, Jarvis's own memory). Not the tool's own `destructive`
flag: the browser marks clicking Send as harmless. `shell_run`, `fs_write`, `fs_edit` are not
offered to a task at all (`tasks.deny_tools`). An approval nobody answers in 4 h is turned down
with "re-read the source before you ask again".

Found on the way: an approval given before a call went out was taken as the answer to "did it
complete?" after a restart, and an approved mail could go out twice. The resumed path now counts
only an answer given after the call went out (`engine/loop.py`, any kind of run).

### "Done" is checked, not claimed

A task ends with `task.finish`: a summary and, per DoD item by number, met or not and the ids of
the calls or files that show it. The core looks each one up in the task's own log: it must be a
call of this task that succeeded, or a file it attached; the task's own bookkeeping is not
evidence; an outward item ("the mail is sent") needs a non-read-only call Arsen approved that did
not become a draft. Anything else is "claimed, not verified", and such a task is at best "partly
done". A run that ends without `task.finish` (budget, supervisor, a machine down) is "not done",
its items "not reported".

### Can I

- `HealthBlock`: what cannot be reached right now and since when, in a chat's turn - only when
  something is down, so it costs nothing otherwise.
- `tasks.delegate` checks the namespaces the brief needs against the real provider status, and
  makes no task when one is down; `begin` checks again when the task starts (a noted task starts
  minutes later).
- A task whose call fails while the registry says its machine is down ends on the spot, "not
  done: X is not reachable (down since hh:mm)", with no wrap-up.
- Knowing enough and understanding the goal are the model's judgement, led by the tool
  description: ask the one question instead of guessing; `task.ask` parks the task "needs your
  answer" and his answer continues it.

### The report

Written into the chat that asked only while no run is working there (a message in the middle of a
run would land in that run's history), after a core note ("[core note, not from Arsen] …") so the
model reads it as the core's, not as Arsen talking. The report's words about each item are the
core's verification, in English; the summary is the task's. Files the task attached move onto
the report. One quiet notice (`type: notice`, never Discord) opens the card.

### Calls

- The call page gives each call an id, sends it with every turn, says `call.alive` every 20 s and
  `call.ended` at hang-up (a keepalive request when the page goes away). A page that said nothing
  for `tasks.call_drop_after_s` (240 s - Chrome wakes a locked phone's timers about once a minute)
  has dropped. Silence on the line never ends a call.
- `tasks.note` takes a title; his words are copied by the core from the turn. The task's first
  step writes the brief from his words and the call's transcript (`task.brief`); nothing else
  runs before it. No brief is written while he waits on the line.
- The noted tasks start exactly once: one `UPDATE … WHERE state = 'noted' RETURNING` on the one
  writer, so a second hang-up, a reconnect or the sweeper finds nothing left. A call that was
  taken for dropped and speaks again is open again, and only what is noted from then on waits for
  its end.
- While a call is open no task starts, and a running one holds between its steps (the held time
  is not budgeted): the voice comes first on the shared GPU. `tasks.max_running` is 1.
- The call's work policy - "do it now, never after the call" - was a sentence inside the voice
  style, and Arsen's stored style (edited 2026-10-20) has it verbatim. At start the core takes
  that one sentence out, once, keeping every line of his (`voice.style_version` 2). With tasks off
  `VoiceBlock` still says it, word for word; with tasks on the tasks block says what replaces it
  and that it takes precedence. A style where the sentence cannot be found is left alone, a
  warning is logged and Settings → Voice says so.
- One after-call report: when the last task of the call has ended or waits on him, one message
  with every task's report, and one notice. A task still waiting reports on its own later.

## Not in this version

- Real phone calls over a SIM (issue #9); sub-agents on outside providers; recurring tasks
  (schedules are that).
- "Wait until the host is back" scheduling: a task needing a down machine is declined honestly.
- Fuzzy detection of the same ask twice: the open and noted tasks ride in the chat's context and
  the model amends instead of duplicating; a double send is one task by its tool-call key.
- Tasks delegating further (`max_depth` is stored for later).
