# Context usage — needs, as-is, plan (2026-11-07)

## Arsen's ask
"Add a context utilization bar in the Jarvis UI split by agents and showing context overflows, so I can
at any moment check how I am utilizing the setup. And a detail line chart where I can see hourly, daily
etc. how I use the context — to check the load and see if I should redistribute scheduled prompts based
on my usage, or I need to add more context to the system. End to end from idea to UAT, autonomously:
commits, deploys, tests, edge cases."

## Needs
1. **Now** — one look, any moment: per model endpoint (GPU box), how much of its context window the
   calls in flight are using, split by who is using it ("agents": Chat, a voice call, each scheduled
   prompt by name, tasks, triage, mail desk, meetings, housekeeping), and whether anything overflowed lately.
2. **Over time** — line chart, hour / day / week / month ranges: peak context per call against the window
   (= do I need a bigger window), load (GPU-busy share of time, tokens processed) per source (= when is it
   busy, which schedules to move), overflow markers.
3. **Overflows** made visible, each kind on its own: the server refused a prompt as too long; Jarvis had to
   drop older history to fit the window; the answer was cut at the output limit.
4. Decision support: a per-source table (calls, peak, typical, busy time, overflows) and the hours of the day
   where the load sits, so schedules can be moved to quiet hours.

## As-is
- Every model call goes through `OpenAICompatAdapter.stream` / `OllamaAdapter.stream` (models/), behind
  one `EndpointGate` per endpoint. Usage (prompt/completion/cached tokens, ttft, duration) is only kept
  summed per run (`runs.usage`) and per main-loop step as `model.call`/`model.done` run events; planner,
  classifier, judge, journal, compaction, knowledge calls leave no per-call record. No time series.
- Window: `AdapterFactory.context_window()` — `num_ctx` on the lane, else vLLM's `max_model_len`.
- Overflow handling exists but is silent: a 400 "too long" is retried with a smaller `max_tokens`
  (openai_compat `_lines`); `ContextBuilder.trim` drops old history to fit; `finish_reason=length`.
- Attribution: run kind / run id in context vars (`current_run_kind`, `current_run_id`); scheduled
  conversations carry `folder_label` = schedule name.
- Web: no chart library; hand-drawn SVG elsewhere. Status screen = instrument panel.

## Design
**Core**
- `features/context_usage.py` — `ContextLedger`: an in-memory set of calls in flight (for the live bar)
  plus one row per finished call in a new table `model_calls` (migration 0021). Written by the adapters
  (one choke point, every caller covered, no plumbing through features). Rows keep endpoint, model,
  role, source, label, run/conversation ids, prompt/completion/cached tokens, window at call time,
  duration, outcome and overflow kind — so history stays right when settings change.
- Attribution from context vars: run kind (+ voice channel) → source; schedule name from the
  conversation's folder label; off-run calls are "housekeeping" with a purpose context var set where
  those callers already set their urgency (journal, compaction, knowledge). Role is passed by the factory.
- Overflow kinds: `rejected` (server refused the prompt as too long, even after the max_tokens retry, or
  the retry itself — recorded as `shrunk`), `trimmed` (history dropped to fit; tokens dropped kept),
  `cut` (finish_reason=length).
- Retention: rows older than `context_usage_retention_days` (setting, default 90) pruned daily.
- API: `GET /api/context/live`, `GET /api/context/usage?range=1h|24h|7d|30d&tz=` (buckets computed in
  the asker's timezone, DST-safe), `GET /api/context/overflows?range=`.

**Web**
- A small meter in the top bar (desktop) → the Context screen; on mobile in "More".
- Context screen: live bars per endpoint (segments by source, colour-stable per source), overflow counter;
  line chart (peak context per call vs window line; or busy %; overflow dots), range tabs Hour/Day/Week/Month,
  source toggles; per-source table; hour-of-day load strip (last 7 days) for moving schedules.
- Polls `/live` every 3 s only while visible; history on range change and every 60 s.

## Edge cases to cover
No data yet; window unknown (show tokens, no %); tokens above window; several endpoints (27B two lanes,
Flash-Next one); failover; cancelled / failed calls; provider without usage (estimate from chars, marked);
DST and timezone buckets; settings changed mid-history; deleted conversations (label kept on the row);
fake adapters in tests (not recorded); ledger write failure must never fail a model call.
