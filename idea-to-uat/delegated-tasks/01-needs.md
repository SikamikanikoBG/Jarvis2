# 01 — Needs: delegated tasks ("I'll get back to you")

## The idea in three sentences
Today every request to Jarvis is a run Arsen watches until it ends; on a call he sits on the line
while Jarvis works. A real assistant says "on it, I'll come back to you", hands longer work to
someone (a sub-agent with the context and a clear Definition of Done) and reports when it is done;
on a call they jot down the asks, work through them after hanging up, then report what got done and
what didn't. Success = Arsen stops waiting: an honest acknowledgement in seconds ("taking it" /
"can't do this one, because …"), and later one clear report per task with the result and evidence,
or an honest "not done, here's why".

## Personas
- **Arsen on a call (voice)** — driving/walking, Android + Chrome, calls Jarvis several times a
  day. Pain: long tool runs make the call go silent; he waits or hangs up and loses the thread.
- **Arsen typing at the laptop (Brave)** — fires off 3–4 asks in a row while working. Pain: one
  long run blocks the chat; he can't move on until it ends.
- **Arsen later, on the phone** — reads push notifications and opens the result. Pain: results
  scattered over chats; nothing says "this one failed, you need to decide X".
- **Jarvis itself (the orchestrator)** — must decide per ask: do it now, delegate, or decline;
  must write a brief good enough that a sub-agent without the chat history can finish it.

## User stories (acceptance criteria Given/When/Then)

**US-1 Delegate instead of making me wait.** As Arsen, I want Jarvis to hand a long task to a
sub-agent and tell me right away, so I can keep talking or leave.
- Given a request that needs many tool steps, When Jarvis decides to delegate, Then within one
  reply it says it started the task and in one line what it will deliver, and the chat/call is
  free for the next ask (the sub-agent runs in the background).
- Quick asks (one lookup, a short answer) are still answered inline. Delegating is the model's
  judgement, not a rule on words.

**US-2 A brief with context and a Definition of Done.** As Arsen, I want each delegated task to
carry the context and a checkable DoD, so the result is what I meant.
- Given a delegation, Then the task holds: goal, the relevant context from the conversation
  (who/what/links/files), constraints, the DoD as checkable items, and what needs my approval.
- I can open the task and read the brief; the sub-agent works from the brief (+ the tools it
  needs), not the whole chat.

**US-3 Report back when there is a result.** As Arsen, I want Jarvis to confirm when a task is
done, so I don't have to chase it.
- Given a sub-agent finished, Then a report lands in the conversation that asked (and a push rings
  the phone): per DoD item done / not done, the result itself (or files.attach), and anything
  waiting on me.
- Given it failed or got stuck, Then the report says so plainly with the reason — never "done"
  for something not done.
- If a task finishes while we're still on a call, Jarvis may mention it at a natural pause.

**US-4 Collect tasks during a call, work them after.** As Arsen on a call, I want Jarvis to note
what I ask for and do it after I hang up, so the call stays a conversation.
- Given a call, When I ask for things that take work, Then Jarvis confirms each briefly
  ("noted: X") and can read back the list ("what have you got so far?").
- When the call ends, Then the noted tasks start (each as a delegated task), and one after-call
  report lists every task: done / partly / not done + why.
- I can amend or drop a noted task during the call ("forget the hotel one").

**US-5 Honest about what it can take.** As Arsen, I want Jarvis to tell me up front if it can't do
a task, so I don't wait for something that will never come.
- Given an ask, Then Jarvis judges three things — **can** (tools/access exist and are up: host
  online, mail account, etc.), **knows enough** (which document, which person, what budget),
  **understands the goal** (what "good" looks like) — and says which is missing.
- Missing context → it asks the one question needed (on a call: now, or noted for after) rather
  than guessing. Not possible → it says so and why, and creates no task.
- The judgement uses the real tool/host status and the model — never a keyword list.

**US-6 See and steer what's running.** As Arsen, I want to see open tasks and their state and
cancel one, so I'm in control.
- "What are you working on?" (voice or typed) lists open tasks with their state.
- A visible list in the web app; cancel works and is reported.

**US-7 Safe when unattended.** As Arsen, I want background tasks to never do irreversible or
outward-facing things without my OK, so delegation can't repeat the 2026-10-15 self-reboot.
- Given a sub-agent reaches an action that needs confirmation (send mail, delete, reboot, pay,
  message a person), Then that step pauses, the task goes "waiting on you", I get a push, and I
  approve/deny from the report.

## Edge cases
- EC-1 Two asks in one breath ("book X and check Y") → two tasks, two lines in the report.
- EC-2 The same ask twice (repeat on the call / double send) → recognised, not duplicated.
- EC-3 Call drops (network) vs. hang-up → noted tasks start exactly once; no double start on reconnect.
- EC-4 Core restart / redeploy mid-task → resumed, or reported as interrupted; never silently lost.
- EC-5 Host offline (jarvisvm/laptop) when needed → honest "can't now", or wait with a deadline — said up front.
- EC-6 Many tasks at once on the shared GPU → queued on the background lane, voice still first; Arsen told it is queued.
- EC-7 Sub-agent needs info only Arsen has → task goes "needs input" with the question, no guessing.
- EC-8 Task finishes while Arsen is on a call in another chat → no interruption mid-sentence; at a pause or by push after.
- EC-9 Incognito chat → its tasks never leak into the day journal / other sessions.
- EC-10 Very long result (research, files) → short report + files.attach; full text kept with the task.
- EC-11 Bulgarian on the call → English report to Arsen; content for others stays in their language.
- EC-12 Cancel while running → stops cleanly, partial work reported.
- EC-13 Task that never ends → budget, wrap-up and a "stopped here" report.
- EC-14 Arsen changes his mind later ("3 nights, not 2") → the running task is steered.
- EC-15 Mobile width + dark mode for the task list and report.
- EC-16 Asked in the Jarvis home conversation vs. a topic chat → the report goes where it was asked.
- EC-17 A sub-agent delegating again → depth limited.

## Out of scope (for now)
- Real phone calls over SIM (issue #9) — this uses today's web call.
- Sub-agents on outside providers (Claude Code, cloud) — unless the as-is map shows a ready path.
- Recurring tasks — that's schedules.

## Open questions
1. **The 2026-09-19 rule** "Jarvis does not strike up conversations with itself" (features/sessions.py):
   a sub-agent is Jarvis talking to Jarvis. Assumed fine now because Arsen asks for it — scoped to
   tasks Jarvis announced to him.
2. Where does a sub-agent's work live — its own conversation, or a child run inside the asking chat? (→ mockup options)
3. Delegation: just announce it (cancel possible), or ask "shall I?" each time? (assumed: announce)
4. After a call: start noted tasks at hang-up, or read the list back and confirm before hanging up?
   (assumed: a short read-back at the end of the call when there are tasks)
5. Concurrency cap for sub-agents on the shared GPU.
