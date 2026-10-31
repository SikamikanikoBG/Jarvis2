# Meeting slots (issue #11) — fast-track needs + plan (2026-10-31)

Arsen needs it today ("today I have to organise many meetings"). Fast track agreed: short needs + plan,
no mockup gate, read-only spike first, build, test, deploy, light alpha.

## Need (Arsen's words, 2026-10-31)
"When I ask about certain users, Jarvis looks at the slots and proposes; I say when and the agenda.
Everything only through the chat, no extra screens."

## Stories
- US-1 As Arsen, I name colleagues (names or addresses) and a window ("this week", "Tue–Wed",
  "45 min"); Jarvis answers in the chat with 2–3 common free slots (his own calendar included).
- US-2 Who has no free/busy data (unresolved name, private calendar, error) is named, not silently
  dropped; slots are then computed for the rest and the answer says so.
- US-3 Arsen picks a slot and gives the agenda; Jarvis sends the invite from Arsen's account
  (existing `calendar_create` with attendees + send_invites) with the agenda in the body.
- US-4 Works by voice too (same tools; read the slots in one sentence each).
Edge: ambiguous name → ask which one (GAL resolve gives candidates); nobody free → say so and offer the
nearest near-misses (who blocks); weekends/out-of-hours excluded by default; tentative counted busy
unless asked; OOF (3) shown; DST-safe local wall clock (as calendar_free_slots does); long windows capped.

## Spike result (read-only, jarvisvm session 1, 2026-10-31)
`Namespace.CreateRecipient(addr).Resolve()` + `Recipient.FreeBusy(date, 30, True)` works headless:
1392 chars = 29 days × 48 half-hours, codes 0 free / 1 tentative / 2 busy / 3 OOF / 4 working
elsewhere. VChuleva, PKarova, NIPetrov all resolved; PKarova busy 12:00–13:30 today.
The issue's Scheduling-Assistant-via-screen plan is not needed.

## Plan
1. Host (`packages/host/jarvis_host/outlook.py` backend + `server.py` tool): new READ tool
   `calendar_free_busy(attendees, start=None, days=5, duration_min=30, work_start_hour=9,
   work_end_hour=18, limit=3, include_me=True, tentative_busy=True)`:
   - resolve each attendee (address or name; ambiguous/unresolved → `no_data` with reason and, if
     available, candidates); Arsen included via CurrentUser;
   - FreeBusy at 30-min (or 15 if duration needs it) granularity, parsed into per-person busy blocks
     inside the window (local wall clock, DST-safe like `calendar_free_slots`);
   - common free slots of `duration_min` on weekdays in work hours, ranked earliest first, up to
     `limit`; if none, the best near-misses with who blocks them;
   - returns {window, people:[{who, address, resolved, busy:[…], no_data?}], slots:[…], near_misses:[…]}.
   - Same COM thread/apartment, retry and error handling as the other calendar tools.
2. Tool descriptions guide the flow in the chat (no new UI): find slots → propose → Arsen picks + agenda
   → `calendar_create(attendees, send_invites=true, body=agenda)`. No keyword lists.
3. Tests: pure parsing/intersection unit tests (FreeBusy string → blocks → slots; DST day; no_data;
   tentative; OOF; nobody free → near misses; limit); fake-COM backend test like existing host tests.
4. Deploy: host version bump; jarvisvm = git pull --ff-only + stop the verified jarvis-host PID on :9030
   (supervisor restarts it; check task "\Jarvis2 Host Daemon" is Running first). Core only if needed.
5. Light alpha (read-only, on production — the tool is read-only): in a Jarvis chat ask for common slots
   with Chuleva, Karova, Petrov next week; check the answer and the tool call. No invite is sent in the test.

## DoD
- [ ] US-1, US-2, US-4 verified through Jarvis chat on production (read-only); US-3 verified by the
      tool path and a unit test (no real invite sent during testing)
- [ ] unit tests for parsing/intersection/edge cases; full suite green locally and in CI
- [ ] host deployed on jarvisvm, healthy (`/api/status` jarvisvm ok), tool listed
- [ ] issue #11 updated; CHANGELOG entry
