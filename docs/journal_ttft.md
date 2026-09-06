# Time to first token — measured, 2026-09-06

Arsen: "sometimes I'm waiting more than one minute, even two minutes until the first token".
Brief: make TTFT as fast as possible with the full context cached, appending only what is new.
No regressions, no hardcoding.

## Where the time actually goes (30 real runs on ardi, from `run_events`)

Decomposed as `run.queued → run.started` (queue), `→ first model.call` (pre-flight: skills,
tier, plan, context assembly) and the chat call's own `ttft_ms` (prefill).

| | queue | pre-flight | model TTFT | prompt tokens |
|---|---|---|---|---|
| all runs, median | 0.00 s | 3.96 s | 4.50 s | 14,770 |
| all runs, p90 | 0.00 s | 48.43 s | 20.34 s | 30,904 |
| chat only, median | 0.00 s | 1.27 s | **9.39 s** | 15,521 |
| chat only, p90 | 0.00 s | 4.72 s | **22.09 s** | 33,256 |

Queueing is never the problem. For chat the cost is prefill.

## The server is not the problem

vader's vLLM: `enable_prefix_caching=True`, `kv_cache_size_tokens=305,834`,
lifetime hit rate 21.4M/26.4M = **81%**. A four-turn probe in a fresh conversation:

```
turn 1 COLD: 13,364 tok | TTFT  782 ms
turn 2     : 13,392 tok | TTFT  274 ms
turn 3     : 13,421 tok | TTFT  432 ms
turn 4     : 13,451 tok | TTFT  434 ms
```

The design works. Marginal prefill measures **~1,200 tok/s**, which is the hardware ceiling for
a 27B on three 3090s — so the only lever is *how many tokens have to be prefilled again*.

## Cause 1 — the tool list changes, and tools render into the system prompt

Turn-by-turn, every miss in Arsen's real chats lands on a change in `tool_count`:

```
Кой е Теди            turn 3  15,329 tok  +137   0.31 s  tools 71  held
                      turn 4  15,958 tok  +470  10.96 s  tools 73  MISS  [tools 71->73]
Програма за седмица   turn 2  33,256 tok +2889  23.73 s  tools 73  MISS  [tools 71->73]
                      turn 3  39,442 tok +1382  20.34 s  tools 71  MISS  [tools 73->71]
Отговор с думата ОК   turns 2-4                 0.27-0.43 s  tools 71  held
```

6 turns held, 3 missed, **17 s lost on average per miss**. `71` is every provider except the
browser; `73` is with it. So opening or closing the browser extension re-prefills every
conversation, because the chat template renders the tool list into the system message and
everything after a changed byte is uncacheable.

Two independent sources of churn:

* **content** — `WsProvider.list_tools()` returns `[]` the moment the extension disconnects, and
  `ws.py` calls `registry.forget()`, so its two tools leave the prompt;
* **order** — `reindex()` rebuilds the index with the re-listed provider's tools appended at the
  END, so a reconnect reorders the prompt even when the set is identical.

The registry already holds the right principle for one case — *"Better the tools we knew than
none: a transient failure must not empty the registry"* — it just does not cover a provider that
answers "I have nothing" while reporting itself disconnected.

## Cause 2 — compressing old tool results rewrites the middle of the prompt

Per-step inside long runs, a shrinking prompt is always followed by a huge TTFT:

```
step 10   86,007 tok   +5,384    6.10 s
step 11   78,542 tok   -7,465   64.20 s   <- everything after the rewrite re-prefilled
step 13   84,380 tok   +4,102   59.44 s
step 14   74,684 tok   -9,696   48.99 s
(another run)  94,041 -> 70,880  -23,161  45.66 s
```

`_compress_old_tool_results` truncates the oldest results in place once they exceed the budget,
and stops as soon as it is back under it — which guarantees it fires again on the very next
step. Each fire invalidates from the first rewritten message onward: ~78k tokens at 1,200 tok/s
is the 64 s observed. Truncating 7,465 tokens saves ~6 s per later step, so firing once can pay
for itself over a long run; firing three times cannot.

## What we can now measure

vLLM returns `prompt_tokens_details.cached_tokens` in the streaming usage. Recording it turns
every one of the inferences above into a number the run inspector shows directly.

## Changes

(filled in as they land)
