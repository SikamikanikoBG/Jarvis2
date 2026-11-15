# 14 — "Change it yourself"

Arsen could change anything on the Settings and Schedules screens; Jarvis could not. Asked to
make the morning brief half an hour earlier, it deleted the schedule and made a new one (losing
its history); asked to turn the hold music down, it said it could not. 2026-11-15: "искам full
create edit delete rights за тези неща и за всичко друго, което аз виждам в интерфейса".

Built 2026-11-15 (issue #14; needs, mockup, panel and plan in `idea-to-uat/agent-admin/`). On by
default: `settings.agent_admin.enabled` off is the rollback.

## Stories

1. **He reads his settings.** "What's your call patience?" is answered with the real value and
   what it means - read from the comment above the field in `jarvis_proto/settings.py`, so the
   meaning Jarvis gives is the one a developer reads.
2. **He changes them.** Validated exactly as the Settings screen is, nothing half-applied, old →
   new on a card in the chat with Revert. A wrong value gets the validator's own words.
3. **He edits a schedule in place.** Same id, history kept: time, prompt, pause/resume, run now,
   its fires.
4. **Everything else the UI does.** Boards, folders, chats, skills, the knowledge graph, meetings,
   triage and RSVP runs - through the UI's own routes.
5. **A trail and an undo.** Every change, by Arsen or Jarvis, in Settings → Changes, with Undo.
6. **In the background too**, within the rules below.

## The rules

**One write path.** `features/admin.py: SettingsAdmin` is the only code that saves settings:
`PATCH /api/settings` from the UI, the `settings.*` tools, a Revert, an applied proposal, the
"Always allow in tasks" button. One lock, one revision (`ETag` / `If-Match`, 409 on a stale
form), one row per change in `admin_changes`, one `settings.changed` event. A change is put onto
the settings in force *at the moment it is saved* (field by field), so a save that landed in
between is never undone - the old `merge_settings` replaced `roles` and every list whole.

**Risk is data on the field** (`settings.marks`), never a list of names in the code that applies
it. The nearest marker on the path counts, and a section's covers what is under it:

| class | marked on | what it does |
|---|---|---|
| `lifeline` | a lane's `provider/base_url/model`, `run_routing`, `mcp_servers`, `voice.namespaces`, `stt_url` | asks; probed before saving (refused if it does not answer, unless it did not before either), checked again after `agent_admin.probe_window_s` and put back by itself if that fails - also after a restart |
| `safety` | `confirmations`, `email`, `tasks.no_approval/deny_tools`, `rsvp.allowed_domains/vip`, `agent_admin` | asks |
| `secret` | `mail_accounts[].app_password`, `discord_webhook_url`, `mcp_servers[].env/headers` | writing one asks; masked in `settings.get` unless `reveal=true`, and in the trail, cards and events |
| `delete` | an item removed from a list or dict; a DELETE route; a route's `risk_when` body value | asks; says what is lost |

The tool tells the loop through a per-call risk hook (`ToolRegistry.assess` → `CallRisk`): the
call then behaves as destructive, so the existing chat card, task approval and spoken yes on a
call do the asking. `confirmations.mode=off` lets risky changes through unasked like any other
destructive call - his choice - but probes and the auto-revert still run: they live in the write
path, not the loop.

**Runs nobody watches** (scheduled, triage, meeting, system) cannot ask. A harmless change
applies; a risky one becomes a **proposal**: recorded, not applied, Apply / Discard on its card
and in Settings → Changes, lapses after `agent_admin.proposal_ttl_s`. Any other risky call there
is not done.

**A run keeps the rules it started with.** Confirmations, the task rules and the e-mail policy
are frozen at the start of each run (`engine/loop._rules`); a change Arsen makes counts at once
(`Core.policy_epoch`), a run's own only from the next run. A run cannot switch confirmations off
and use that itself.

**Secrets.** Gate A: full access - Jarvis may read and set them. `reveal=true` is for when he asks.
After a run has read one in clear, any call carrying that exact value anywhere but back into the
settings asks (refused in a run nobody watches) - compared as data, the stored value itself. The
day journal and the knowledge learner never see a stored secret (`SettingsAdmin.redact`).

**Schedule prompts and authorship.** `Schedule.prompt_by`: null when Arsen wrote the prompt (in
the UI, or Jarvis at his asking in his own chat), else the run that wrote it. Such a prompt fires
*asking like a task* until he accepts it (the card's "Accept prompt", the Schedules screen's
"by Jarvis · Accept", `POST /api/schedules/{id}/accept`): content read by a run must not become
standing authority nobody saw. Name, time and pause stay free. A fire cannot run its own schedule
again.

**Collab runs never get these tools** (`ToolSpec.owner_only`): another agent with a collab key is
not Arsen.

**Routes opt in.** `jarvis.api_*` calls the UI's handlers in process (`httpx.ASGITransport`) - same
validation, events and answers. A route declares itself with `openapi_extra=agent("read"|"write"
|"none", risk=, risk_when=, prefer=)`; unmarked is unreachable, and `tests/test_agent_routes.py`
fails on any `/api` route with no decision, so a route added later forces a yes or no in its own
diff. Never: collab keys, `allow-tool` (a run must not answer its own question), registering a push
device, `/stt`, `/tts`, `/voice/*`, uploads, applying a proposal, accepting a prompt. `prefer`
sends settings and schedules to their precise tools, so there is one trail and one probe path.
A run cannot delete the conversation it is speaking in.

## Ways back, none needing a model

- the change card's **Revert** (field by field; refused if a field changed again since - revert the
  later one first);
- Settings → **Changes** → Undo last change / Revert / Apply;
- `POST /api/settings/changes/{id}/revert`;
- break-glass on ardi with the core down: `python3 scripts/admin_revert.py --last` (runs
  `jarvis_core.admin_revert` in the image against the data volume), then restart the core.

What a change already caused while it was on (RSVP answering for an hour) stays done; the card
says so.

## Size

Ten tools (`settings.get/set/revert`, `schedule.get/update/run_now/fires`, `jarvis.api_describe/
api_read/api_write`), 5.5k characters of spec together - about 1.7k tokens (a test keeps them under 7.2k). The route
catalogue is never in the prompt: `api_describe` returns it on demand.

## Not done (yet)

- The Settings form sends whole top-level sections it changed (as before); a stale form is caught
  by `If-Match` and the "changed elsewhere" banner rather than by field-level PATCH.
- A strict scheduled run waiting on an approval shows in the chat list like any waiting run; it
  sends no push of its own.
- `tasks.deny_tools` is read live by the task gate, not from the run's frozen rules.
