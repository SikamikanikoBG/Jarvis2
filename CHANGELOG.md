# Changelog

Notable changes to Jarvis2. Older history lives in the commit log, `docs/journal.md` and `docs/WAVE2.md`.

## 2026-11-23 — core 2.0.0a133

From the TTFT review of 22 Nov (27b on vLLM since 09:16) and long-term memory step 1 (agreed 22 Nov).

### Fixed
- **A screenshot no longer makes the model read the last 13k tokens again.** Past the six-picture
  limit every new picture dropped the oldest, which rewrote the prompt where that one stood: 9 of 9
  screenshots past the limit in one 22 Nov run fell back to the cache before it (17.6 s each; not one
  other step of the day fell back). Old tool pictures now leave half the limit at a time, so the next
  ones are appended. What Arsen attached, and what the current step captured, is not dropped early.

### Added
- **`memory.recall`: what was said, in every conversation, by words and by day.** Migration 0023
  indexes what Arsen wrote or said, what meetings heard and what Jarvis answered (full text, SQLite
  FTS5; prefixes, so inflected forms are found) and keeps it in step with the messages. Private chats
  are never searched, the question being asked is not its own answer, and it is not offered to another
  agent's runs. Tool results, steps that only call a tool and the core's own notes are left out.

## 2026-11-22 — core 2.0.0a132

From the TTFT review of 21 Nov (flash-next on llama.cpp, a hybrid model: it resumes a prompt only
from a context checkpoint, the first of which sits at the first user message, right after the
tool schemas and the system message).

### Fixed
- **Every scheduled run starts from the same cached prefix.** The schedule's name stood in the
  system message ("## This run … '<name>'"), so no two scheduled runs had the same system text and
  each first call re-read all ~31k tokens of tools and system (cached 0 on 6 of 6 first calls,
  TTFT p50 23 s, 22 Nov 01:00–05:00; chats, whose system text is identical, resume at 28,881). The
  rules stay in the system message, worded the same for every schedule; the name is now a
  `ScheduledRun` block in the run's own context.
- Also measured: the a131 turn-taking works. The 22 Nov night batch (11 runs) ran one at a time;
  scheduled main-loop TTFT p50 3.4 s / p90 19.5 s against 39.2 s / 90.6 s for the same batch on
  21 Nov, which still ran on a130.

## 2026-11-21 — core 2.0.0a131

From the TTFT review of 20 Nov (flash-next on llama.cpp, `-c 98304 -np 4 -kvu`).

### Fixed
- **The clock's runs take turns by run, not by step, on a shared KV pool.** Up to three scheduled
  runs worked at once for 5 h and took turns step by step on one 98k KV pool that cannot keep two
  ~55k prompts: every step after another run's step re-read ~42k tokens (TTFT p50 55 s against
  2.3 s after its own step; 48 % of all main-loop TTFT time that day). Scheduled and triage runs now
  start only when the clock's runs already on that lane leave a whole window (`num_ctx`) of the
  server's shared pool free (vader: one at a time). Chat, mail, voice, meetings and tasks are not
  held; on vLLM (no shared pool) nothing changes. A run that parks on a question now wakes the queue.

## 2026-11-17 — core 2.0.0a130 / host 2.0.0a35

From the FX/MT5 task of 17 Nov (one hour, 127 steps, 9.5M prompt tokens, no order placed).

### Fixed
- **jarvisvm can get its screen back.** The host is not elevated, so its one-shot SYSTEM task for
  `tscon` failed ("Access is denied") and every `screen_*` tool with it. It now runs the
  `Jarvis2 host console` task that `scripts/install-console-task.ps1` installs once (elevated);
  until that is installed the tools say so instead of "Access is denied". `host_status.desktop`
  reports session, console and whether the task is installed.
- **Programs that must keep running, and the dialog in front of them.** New host tools: `app_start`
  (detached, outlives the call; reports whether it still runs and its windows, dialogs called out)
  and `app_windows` (by pid or name, works with nobody at the screen). MT5 had sat behind a "Login"
  dialog nobody could see while its Python API timed out.
- **A long run no longer forgets what it read.** When a run outgrows its context within its own turn,
  its ask stays first, the cut lands on a call (never an orphaned result), and a pinned index lists
  every dropped step with its `@ref`, so it is read back instead of run again (it re-read the same
  file regions ~15 times).
- **Skill triggers match as phrases, not inside words** ("x.com" fired inside "forex.com"), and the
  skill picker needs the request to be about the skill, not to share a word with it.

### Changed
- **A hand-off is his words, not the chat's spec.** `tasks.delegate` refuses more than 1,200
  characters of goal/context/constraints/DoD; the brief shows his words first and marks the chat's
  reading as such (his words win, extras optional, claims about machines/files to be checked); the
  DoD is what he asked, no wider; tasks take the simplest path on what is already there first.
- **The supervisor judges against his words** (not the first 600 characters of the brief), and being
  blocked on something only Arsen can give is a nudge to ask him, never a stop.

## 2026-11-16 — core 2.0.0a129 / extension 2.5.0

### Fixed
- **A click that opens its result in another tab is seen.** A popup, a `target=_blank` link or a named
  window the site reuses used to come back as "Clicked" and then "Nothing changed in 15s". On 16 Nov a
  Postbank course's «Продължи курса» opened its player through a Microsoft sign-in popup and two runs
  never learned it existed. `browser.click`/`type` now report the tab the action opened or sent to a
  new page ("This opened a new tab: [tab N] … Carry on there with tab=N"), and `browser.wait` returns as
  soon as one appears.
- **`browser.eval` works on every page.** Without `page_world` it ran in the isolated world, where MV3
  forbids compiling strings on every site (the "CSP" in its refusal was the extension's own). It now runs
  as a user script, which no page CSP can block, once "Allow user scripts" is on for the extension;
  until then the refusal says how to turn it on.
- **A page that failed to load says why** (`net::ERR_…` and the URL) instead of "Frame with ID 0 is
  showing error page", in reads, clicks and `browser.open`.
- **The supervisor no longer suggests what already failed, or typing credentials.** It is shown the
  tools whose last call failed in the run, they are no longer offered as the way out, and a step only
  Arsen can take (approve a sign-in, a captcha, a code) is answered with "ask him and wait".

### Changed
- **A delegated task asks for a step only Arsen can take and carries on**, rather than cancelling the
  prompt and ending "not done"; and it starts from the tab where the page already is (his, or the
  asking chat's) instead of opening it afresh. `browser.tabs`/`open` say the same to every run.

## 2026-11-16 — core 2.0.0a128 / proto a44

### Fixed
- **A chat call beside a background call no longer overruns llama.cpp's shared KV pool.** With `-kvu`
  all slots of a llama-server share one pool (vader: 98,304 tokens, 4 slots). At 11:36 a chat call
  (49k prompt) let in by the reserved interactive slot beside a scheduled call (65k) asked it for
  114k; the server answered "Context size has been exceeded" to both and both runs failed. The pool
  is now read from the server (/props, /slots, /v1/models: slots whose n_ctx add up to more than
  the context share it) and a call is admitted only if its estimated prompt plus output room fits
  beside the calls in flight - interactive still first, a call alone always admitted. If it runs
  out anyway beside other calls, the call waits for the endpoint to itself and goes again once
  (in the adapter before any output, else the run loop retries the step: "chat (again, alone)");
  a lone overflow fails as before. vLLM and Ollama are unchanged. `interactive_reserved_calls` 1
  is safe again.

## 2026-11-16 — core 2.0.0a127 / proto a43 / web alpha.93

### Changed
- **Delegated tasks have the rights of the chat.** A task is offered the chat's tools (the default
  `tasks.deny_tools` is now empty, so host shells and file writes are no longer held back) and asks
  exactly what the chat would under Confirmations - the extra floor that made every destructive-annotated
  tool ask in a task even with the mode off is gone. Since 1 Nov most task approvals were VM screen
  clicks/typing, browser clicks and schedules, and shell/file work was refused outright. `no_approval`
  and `deny_tools` remain for tightening tasks only; risky settings changes still ask.

## 2026-11-16 — core 2.0.0a126, proto 2.0.0a42, web alpha.92

### Changed
- **A chat reply no longer waits behind a background model call.** Calls Arsen waits for (chat, collab,
  voice: `interactive_run_kinds`) take the next free slot at the endpoint before any background call
  (first come, first served within each class) and may use `interactive_reserved_calls` (default 1)
  extra calls in flight that background never takes. 2026-11-15: chat calls waited up to 323 s
  behind 4-6 minute task calls at `max_concurrent_runs_per_endpoint` 1 (which gates calls, not runs).
- **Per-provider speed levers.** New provider `llamacpp`; a `vllm` endpoint whose /v1/models says
  `owned_by: llamacpp` is treated as one. llama.cpp: `cache_prompt` and `id_slot` slot pinning
  (`slot_pinning`, `interactive_slots`, slot count read from /props) so a task never evicts the
  chat's cached prompt. vLLM: `priority` only with `priority_scheduling` (servers on
  `--scheduling-policy priority`; refused priorities are dropped and remembered). Ollama:
  `keep_alive` defaults to 30m and one `num_ctx` per box, so the model is not reloaded.

## 2026-11-15 — core 2.0.0a125, proto 2.0.0a41, web alpha.91

### Added
- **Jarvis changes its own settings, schedules and anything else the UI can** (issue #14,
  docs/stories/14_agent_admin.md). New tools: `settings.get` (any setting, with what it means, from the
  comment above the field), `settings.set` (validated like the Settings screen, all or nothing),
  `settings.revert`; `schedule.get`, `schedule.update` (same id, history kept: time, prompt,
  pause/resume), `schedule.run_now`, `schedule.fires`; and `jarvis.api_describe / api_read / api_write`,
  which call the UI's own routes in process for boards, folders, chats, skills, the knowledge graph,
  meetings, triage and RSVP runs. Not offered to collab runs. Flag: `settings.agent_admin.enabled`.
- **A change card in the chat** under every call that changed something: old → new, Revert, and
  Apply / Discard for a proposal. The approval card shows the same diff.
- **Settings → Changes**: who changed what (Arsen or Jarvis), Undo last change, Revert, Apply. It needs
  no model. Break-glass with the core down: `scripts/admin_revert.py --last` on ardi.
- REST: `GET /api/settings/changes`, `…/{id}`, `…/{id}/revert|apply|discard`,
  `POST /api/schedules/{id}/accept`; `ETag` / `If-Match` on `/api/settings`; WS `settings.changed`,
  `change.recorded`; `/api/status` → `admin`.

### Changed
- **Only risky changes ask, and risk is a marker on the field** (`lifeline`, `safety`, `secret`, plus
  removing an item): a lane's endpoint, MCP hosts, confirmations, approved senders, secrets ask; patience,
  RSVP times, names apply at once. A lifeline is probed before it is saved and put back on its own if
  its re-check fails (also after a restart).
- **`schedule.create` no longer asks** (Gate A: only risky writes ask); `schedule.delete` asks and names
  the fires lost. `confirmations.always_ask` still makes create ask.
- In a scheduled, triage, meeting or system run a risky change becomes a proposal for Arsen instead of
  being applied. A run keeps the approval rules it started with. A schedule prompt a run wrote fires
  asking like a task until Arsen accepts it.
- The Settings screen refreshes when something else changes the settings, and warns instead of saving
  over a newer value (409).

### Fixed
- Two saves of the settings at once (the UI and anything else) no longer lose one of them; every save
  goes through one locked path and is applied onto what is in force.
- A schedule's thinking override can be set back to the default (`think` / `think_level` null), in the
  UI too. `DELETE /api/schedules/{unknown}` is 404. A one-shot `at` more than 5 minutes in the past is refused (422).

## 2026-11-15 — core 2.0.0a124, proto 2.0.0a40, web alpha.90

### Fixed in the alpha, before this release (call patience, round 1)
- **A turn is no longer sent while you have already started your next sentence.** The "sure" decision
  lands 0.7–1.1 s after a sentence ends, a breath is 0.3–0.8 s, and only speech the segmenter had
  confirmed stopped the send: a 69 s briefing went out as 8 sends and 3 runs. Now any sound over the
  floor holds it until its first words decide (no words: the wait goes on with what was left); noise
  that never becomes speech is let go of after 1.5 s.
- **The first words of a sentence are no longer cut off** when the attack started over on a gap
  inside it ("Чакай." over Jarvis, the words after a send): an utterance starts at its first sound.
- A piece of a long turn is recognised with the piece just before it as Whisper's prompt.
- The locked call screen on the light theme: dark-theme colours for the slides and the amber too.
- Hanging up mid-turn is in the call trace (`turn.send why=hangup`); an incognito call keeps no
  voice-debug trace or clips.
- **A short sentence said while Jarvis thinks or speaks is no longer lost** (round 2): over the hold
  music a 1.2 s "Чакай, спри за малко." never held the longer attack and was thrown away unheard. A
  sound long enough to be words is now recognised however it ended, and Jarvis does not start
  speaking while you make a sound or while your words are still being recognised; an answer written
  meanwhile waits, and is dropped if your words go to the run.

### Changed
- **On a call Jarvis lets you finish a thought** (issue #12). The turn model now decides both ways: a
  pause it hears as finished is still sent at once; a pause it hears as unfinished waits for you, the
  less finished the longer, up to the new **Patience when it hears you are not done** (Settings →
  Voice, 1–8 s, default 3 s). A finished question scored just under the bar waits no longer than
  before (1.1 s). With turn detection off the patience is the whole rule; while the model cannot say
  (loading after a deploy) the wait is the old 1.1 s. What you say inside the wait joins the same
  turn, in order; a cough or the room does not hold it. The 1.5 s cap and the 30 s cut are gone: a
  long briefing is one turn, recognised in 20 s pieces while you talk, so the answer is no slower at
  the end (a turn is sent after 5 minutes whatever happens). While it waits the call screen says
  **"Listening — go on"** with an amber ring running down, and nothing is read out over your pause.
  Until now a thinking pause was sent after about a second whatever the model heard: between 25 Oct
  and 14 Nov 31 of 368 voice turns got more of Arsen's words within 20 s of going, often
  mid-sentence, and he had to "talk in portions".
- Hanging up in the middle of a turn sends what was recognised (waiting up to 5 s for pieces still
  with Whisper); the answer lands in the chat.

### Fixed
- Muting or switching earpiece/speaker mid-sentence no longer drops what was said: it is closed,
  recognised and kept (mute sends it at once). If speech recognition fails on one piece, the pieces
  already recognised are kept and the problem is shown once.

### Added
- `voice.turn_patience_s` (1–8, default 3). `scripts/voice_turn_stats.py`: read-only share of voice
  turns that got more of his words within 20 s of being sent, for the before/after.

## 2026-11-15 — core 2.0.0a123 / proto a39 / web alpha.89

### Changed
- **Thinking chip takes effect on a reply that is already working.** Changing it mid-reply sends
  `run.think` to the run: its next model step thinks the new way (even a step adaptive thinking would
  have called mechanical), the step streaming now is not interrupted, and the next messages follow it as
  before. Until now the chip only reached the next message, so a reply that needed more (or less) thought
  had to be stopped and asked again.

## 2026-11-07 — core 2.0.0a116 / web alpha.88

### Added
- **Context screen** (header meter, and More → Context on mobile): how much of the model's context
  window is in use, by whom, now and over time. Every model call (main loop, planner, classifier, judge,
  journal, compaction, knowledge) is booked by the adapters into a new `model_calls` table, attributed to
  its source: Chat, Voice calls, each scheduled prompt by name, Tasks, Triage, Mail desk, Meetings, and
  Housekeeping by purpose.
  - **Now**: one bar per endpoint (GPU box) against its window, a segment per call in flight, calls
    queued for the endpoint, and the last 15 minutes' peak and busy time. The header meter shows the
    fullest endpoint and turns red when anything was refused in the last 24 h.
  - **Overflows**, each kind on its own: *Refused* (the server said the prompt does not fit; vLLM and
    llama.cpp messages), *Answer room cut* (fitted only after a smaller `max_tokens` retry), *History
    trimmed* (older conversation dropped before the call, with how much), *Answer cut off* (stopped at the
    output limit). Each links to its conversation.
  - **Over time**: Hour / 6 h / Day / Week / Month / 90 d, as peak context per call against the window
    (dashed), busy share of time (with each endpoint's total), or tokens processed; overflow dots; a
    per-source table (calls, typical, p95, peak and % of window, busy, overflows); and a "typical day"
    of busy minutes per hour over the last 7 days with the quietest hours, for moving schedules.
    Buckets follow the browser's clock; days are local midnights (DST days are 23/25 h).
- `GET /api/context/live`, `GET /api/context/usage?range=&tz=`; setting `context_usage_retention_days`
  (default 90, pruned every 6 h).

## 2026-11-04 — core 2.0.0a113

### Fixed
- Web search is no longer stuck "not available". One empty search while some SearXNG engines were
  suspended (Brave rate limited, DuckDuckGo CAPTCHA) marked search as down, and only a successful
  search cleared that mark. Meanwhile every chat told the model not to search, so nothing ever cleared
  it. Jarvis answered "web search is not working" for the rest of the day, while SearXNG kept answering
  normally. Now an answer from SearXNG always means search is up. An empty answer names the engines
  that were silent. Only an unreachable or broken backend marks search as down, the mark expires after
  5 minutes, and the registry reads web's status live instead of keeping the copy from its last call.

## 2026-11-04 — core 2.0.0a112

### Fixed
- `tasks.list` now also shows the tasks that ended in the last day, with how they ended, beside the
  open ones, and says `tasks.steer` starts one again. It listed open tasks only, so on 3 Nov, when a
  candidate-research task had stopped half-way (its model host was down), Arsen's "finish this
  morning's task" on a call got "No open tasks" and Jarvis said it had no such task.

## 2026-11-03 — core 2.0.0a111

### Fixed
- A playbook update now shows the model the WHOLE existing playbook (it saw only the first 2,500 chars,
  cut mid-word) and tells it the body replaces the file, so sections past the usual length are carried
  over. One too long to show whole (over 12,000 chars) is left alone instead of rewritten from a cut copy.
  Every update had been chopping the tail: the dev.to playbook lost Arsen's voice rules and ended at
  "Vary the op", and the 2 Nov dev.to run spent ten minutes searching disks and the network for the rest.

## 2026-10-31 — core 2.0.0a110

### Fixed
- A task's report now carries the result written beside its first `task.finish`. On the live core the
  eight-meeting plan wrote its table there, re-finished to fix an evidence ref, and ended with "the full
  proposal is in my message above" - the report showed only that line, so Arsen never saw the table.

## 2026-10-31 — host 2.0.0a33, core 2.0.0a109

Arsen asked Jarvis for a four-week options table for eight meetings (him + Gyuzelev + six colleagues,
Karova optional). Jarvis did not deliver it; these are the general causes.

### Added
- **`calendar_plan_meetings`** (read-only host tool): several meetings in one call - `meetings` =
  `[{"with": ..., "count": n}]`, `always` (in every meeting), `optional` (never block). Returns one
  non-overlapping slot per meeting (scarcest first, repeats `min_gap_days` apart, preferring times the
  optional people can make), every common window per meeting, each person's unavailable days, and what
  could not be planned and why. Computed in code; the model only presents it.
- `calendar_free_busy`: `optional` people (invited, never block) and `all_windows` - every stretch where
  the meeting fits, with who of the optional people can come.

### Changed
- Free/busy now gives each person's busy time **complete**, as `busy_by_day` lines. The list used to stop
  at 40 blocks, so over four weeks the later weeks read as free.
- Arsen's own busy lines name the meeting holding them (his tentative webinars by subject).
- A bare alias ("nstoyanov") that the address book cannot place is tried as an address on Arsen's own
  domain; "nstoyanov" and "spetkov" had come back "not resolved".
- Everyone is read in one Outlook pass; ten parallel per-person calls had timed Outlook out.
- Placeholder blocks Arsen marks **Free** in Outlook never count (they already did not; this is the agreed
  way to mark them).

### Fixed
- A step that spends its whole output allowance on reasoning is followed by a thinking-off step **every
  time**, not once per run. The slot task burned the allowance a second time, fell through to the plain
  empty-reply nudge with thinking still on, and failed with nothing.

## 2026-10-31 — core 2.0.0a108, proto 2.0.0a38, web 2.0.0-alpha.87

### Changed
- **Task approvals follow your confirmations** (UAT, "inherit + small floor"): a task asks what a chat
  would under Confirmations (mode, always allow, always ask), plus one floor - a tool annotated
  destructive still asks in a task when the mode is off. Browser clicks no longer ask with the mode off.
- **"Always allow in tasks"** on a task's approval card: approves the call and adds the tool to
  `tasks.no_approval`, listed (and removable) in Settings → Tasks. Never for a `deny_tools` tool.
- A tool provider that is not set up at all (no mail account, no search engine) offers no tools, is not
  listed as "not available", and shows "not set up" on Status.

### Fixed
- A Gmail clean-up task was declined as "mail is down (no mail account is configured)" although the
  mail is reachable through Outlook on a host: the core's own unconfigured IMAP `mail` was offered and
  named as the need. A need now has to name a namespace that is set up.

## 2026-10-31 — host 2.0.0a32

### Added
- **Meeting slots** ([#11](https://github.com/SikamikanikoBG/Jarvis2/issues/11)): new read-only host tool
  `calendar_free_busy`. Name colleagues (addresses or names) and a window in the chat; Jarvis reads their
  Exchange free/busy plus Arsen's own and proposes 2–3 common slots on weekdays in work hours, earliest first.
  - Tentative counts as busy unless asked otherwise; out of office blocks; "working elsewhere" does not block
    and is named on the slot.
  - Anyone without data (unknown or ambiguous name, no access, error) is named with the reason, never silently
    dropped; for an ambiguous name Jarvis asks which person.
  - Nobody free → the nearest near-misses, each with who blocks it.
  - Arsen picks a slot and gives the agenda; Jarvis books it with `calendar_create` (attendees, invites sent,
    agenda in the body). Chat and voice only, no new screen.

## 2026-10-30 — core 2.0.0a106, proto 2.0.0a37, web 2.0.0-alpha.86

### Added
- **Delegated tasks** ([#10](https://github.com/SikamikanikoBG/Jarvis2/issues/10), story
  `docs/stories/13_delegated_tasks.md`), behind `settings.tasks.enabled` (Settings → Tasks, off by default):
  - Jarvis hands longer work to a background task and answers in one line within seconds; the chat stays free.
  - A task card in the chat that asked (state, Definition of Done, assumptions, brief, cancel, steer,
    approve/deny) and a `Tasks · N` chip; the task's own work conversation opens from the card.
  - The report lands in the asking chat with the result itself (long results attached) and a quiet push.
  - "Done" only with evidence the code checked in the task's own log; otherwise "claimed, not verified".
  - In a task every non-read-only step waits for Arsen's approval, whatever `confirmations.mode` says;
    shell and file writes are not offered to tasks; approvals expire; a denied step is not asked again.
  - Honest take / ask / decline from real health (what is down or not configured).
  - On a call: asks that take work are noted (live list on the call screen), read back when Arsen wraps
    up, started exactly once after hang-up or a dropped call, and reported after the call.
  - One task runs at a time; none starts during a call and a running one pauses between steps.

### Changed
- The stored call style lost its "never after the call" sentence (Arsen's other edits kept).
- On a call, answers with no tool result behind them are no longer learned into the knowledge graph.
- Status shows web search / mail as not ok when nothing is configured.
- The desktop top bar never outgrows the window.

### Fixed
- One-off reminders fired two hours late (a `+02:00` time compared as text against UTC).
- After a restart, an approved but interrupted outward call was re-run without asking again.
- A shutdown in the middle of a model step was recorded as a cancel instead of an interruption.
- A partial nested settings PATCH reset sibling fields.
- `RunKind.MAIL` had no priority, lane or budget.

### Known issues
- A correct task can still end "partly done" when a DoD item describes the output itself (N-8).
- A table sent before the task's last step can reach the report only as prose.
- The task status count can over-report running tasks while one waits for approval.
