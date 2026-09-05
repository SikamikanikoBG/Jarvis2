# 08 — Meeting auto-RSVP

Ported from V1's `meeting_responder_service.py`; same policy, no approval queue (V2 has none),
every action visible in a dedicated conversation, dry-run first like triage.

## Stories

1. **As Arsen**, when a colleague from my organisation invites me to a slot where I am free,
   Jarvis accepts within a few minutes so the organizer is not left waiting on me.
2. **When the slot clashes with a meeting I have committed to** (organised or accepted, busy),
   Jarvis declines and names up to three free alternatives inside my working hours over the
   next few days, so the organizer can re-plan without a round-trip.
3. **An invite from a VIP is never declined.** If it clashes, Jarvis accepts and tells me the
   double-booking is mine to resolve.
4. **An invite from outside my organisation is never answered automatically**; Jarvis leaves
   it for me. VIPs are exempt (an external boss is still a boss).
5. **Tentative or unanswered invites never count as conflicts** — only meetings I am really
   committed to can make Jarvis decline something.
6. **A recurring series or a re-surfaced invite is answered once**, not on every poll and not
   again after a restart.
7. **Cancelled meetings disappear from my calendar** so it stays honest (organizer called it
   off → item removed, nothing is sent).
8. **Before switching it on**, I can dry-run it: see exactly which invites are pending and what
   Jarvis would do with each, with zero sends.
9. **Everything Jarvis did is on record**: one conversation per day under "Calendar RSVP" with a
   line per decision, and the RSVP state (last run, counts, last error) in the API/UI.

## Definition of done

- Host tools: `calendar_invites` (pending, with the committed-only conflicts of each),
  `calendar_respond` (accept | tentative | decline, plain-text comment), `calendar_free_slots`,
  `calendar_remove_canceled`; all covered by tests on the fake COM world.
- Core: `MeetingRsvpSettings` (enabled, host, account, lookahead_days, allowed_domains, vip,
  remove_canceled, work hours), `RsvpJob` with a persisted ledger keyed
  `organizer|subject|start`, `POST /api/rsvp/run?dry_run=`, `GET /api/rsvp/state`.
- Dry run against the real mailbox reads right before `enabled: true`.
