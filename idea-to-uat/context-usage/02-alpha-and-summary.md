# Context usage — alpha + summary (2026-11-07)

## Built
Context screen (header meter → screen; mobile: More → Context) with live per-endpoint bars by source,
queued calls, 24 h overflows by kind (Refused / Answer room cut / History trimmed / Answer cut off),
history over Hour…90 d (peak context vs window, busy %, tokens processed, overflow dots), per-source
table, typical-day profile with the quietest hours. Data: one `model_calls` row per model call,
written by the adapters (models/ledger.py), aggregated in features/context_usage.py.

## Tests
- core: 20 new (`test_context_usage.py`): row per call with usage/window, live estimate while
  streaming, waiting vs running behind one endpoint slot, llama.cpp 400 refusal + "context size has
  been exceeded" → rejected, vLLM 400 + retry → shrunk, finish=length → cut, early close → cancelled
  and the slot freed, no-usage server → estimated, Ollama booked, attribution (scheduled by name,
  voice, housekeeping purpose inside a run), trim carried by the next call only, a broken sink never
  fails a call, buckets/peaks/busy split across buckets/endpoint busy = union, empty range, local
  midnights across the 2026-10-25 DST change, 3 h alignment, retention prune, live listing, API
  validation. Full core suite 615 passed.
- web: 7 new helper tests; full web suite 222 passed; lint + typecheck + build clean.

## Alpha (throw-away core :9121 on ardi, real Flash-Next on vader)
- A real chat: the live view showed the classifier (115 tokens) then the main step (6.6k, growing
  while streaming); rows booked as chat / housekeeping:knowledge / housekeeping:journal.
- A schedule "Alpha news brief" → Run now: booked as `scheduled:Alpha news brief`; with max_tokens=64
  the capped answers appeared as "Answer cut off" overflows in the 24 h counter, chart dots and table.
- Playwright: desktop 1366 (dark, during a running reply, queued call visible) and mobile 390 (dark,
  light): no horizontal page overflow, no console/page errors.
- Found + fixed in alpha: Context in the desktop header pushed nav items off; cards had no padding;
  legend chips unstyled; the y axis jumped to 200k for a 98k window; sparse data invisible (now points).

## Known / left
- The desktop header nav already scrolls at 1366 px wide (it did before this change: 1101 px of nav
  in 1009); the meter takes ~110 px more. At 1920 it fits.
- Not provable headless: how it looks in Arsen's Brave during his own replies (UAT).
- History starts empty: the chart fills as calls are made; nothing was back-filled.

## UAT for Arsen
Open the meter in the header (or More → Context on the phone) while Jarvis answers; switch the ranges
after a day of use; check the "Typical day" quiet hours before moving a schedule.
