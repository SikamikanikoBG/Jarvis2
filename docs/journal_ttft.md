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

## Cause 3 — pre-flight asked the model two questions one after the other

Once prefill was fixed the wait moved. Time to first token, measured end to end, was ~1,300 ms
of which ~950 ms was pre-flight: skill detection and the plan-tier decision, two independent
questions about the same sentence, sent as two sequential round trips. `planner.py` has said
"tier + skills in one cheap call" since it was written; the code never did it. Nothing streams
while they run, so all of it is Arsen watching an empty screen.

There are 40 skills installed and **none has triggers**, so the structural shortcut never fires
and the classifier call happens on every single message.

## Changes

1. **`cached_tokens` recorded** (core a19, proto a9). vLLM's own count of what its prefix cache
   served, in `ModelUsage` → `model.done` → the run inspector shows `cached 12.4k (94%)`. Every
   number below is measured with it, not inferred from timings.
2. **The tool list stopped churning** (core a19). `specs()` is sorted by name, so a reconnect
   cannot reorder the prompt; a provider that answers "nothing" while reporting itself not
   connected keeps its tools, because closing a browser is not losing a capability.
3. **Compression stopped re-firing** (core a20). It comes back to 70% of the budget instead of
   just under it, so the one edit that costs a full re-prefill happens rarely enough to pay for
   itself.
4. **Pre-flight goes together** (core a21, proto a10). `asyncio.gather` over the two questions —
   same prompts, same answers — and `max_concurrent_runs_per_endpoint` 1 → 2, without which the
   gather is a no-op.

## After (same box, same model, measured the same way)

Browser extension arriving and leaving mid-conversation, the exact case that cost 17 s a turn:

```
1 baseline            prompt 13,444 | cached  93% | TTFT   820 ms
2 baseline            prompt 13,470 | cached 100% | TTFT   395 ms
3 extension CONNECTED prompt 13,496 | cached 100% | TTFT   225 ms
4 extension still on  prompt 13,522 | cached 100% | TTFT   220 ms
5 extension GONE      prompt 13,548 | cached 100% | TTFT   275 ms
6 still gone          prompt 13,574 | cached 100% | TTFT   221 ms
```

Time to first token, end to end, as the endpoint limit was raised:

```
limit 1   median 1,181 ms      limit 2   median 992 ms      limit 3   median 803 ms
```

And the property Arsen asked for — only the missing chunk is prefilled — as a conversation grows:

```
turn 1  prompt 14,183 | cached 88% | prefill 1,639 | first token 3,395 ms
turn 4  prompt 16,718 | cached 95% | prefill   846 | first token 2,482 ms
turn 8  prompt 20,098 | cached 96% | prefill   850 | first token 2,544 ms
```

Prefill is flat at ~850 tokens — exactly the new turn — while the conversation grows by 6,000.
It is O(what is new), not O(how long the chat is).

| | before | after |
|---|---|---|
| chat TTFT, median | 9.4 s | 0.2-0.4 s |
| chat TTFT, p90 | 22.1 s | ~0.4 s |
| worst observed in a run | 64.2 s | — (the repeated rewrite is gone) |
| first token, end to end | 12-40 s | 0.8-1.3 s |

## Still on the table

* The **date line** lives in the system message, so the shared prefix changes at midnight: one
  ~9 s prefill for whoever chats first that day. Moving it into the per-turn context (which sits
  at the end) would make the system prefix stable forever, at the risk of the model reading an
  older date from an earlier turn. Not worth the correctness risk without thinking it through.
* **Skills have no triggers.** 40 skills, 0 triggers, so the structural shortcut in
  `SkillDetector` never fires and every message pays for a classifier call. Adding trigger
  phrases to the skills that have obvious ones removes that call entirely for those messages.
  That is data, not code, and it is Arsen's to write.
* `max_concurrent_runs_per_endpoint` 3 measured better (803 ms) on an idle box. It is one
  setting away, and the reason it is not the default is written next to it.


## 2026-11-16 — chat behind a background call (core 2.0.0a126)

Live core, last 24 h: chat calls waited up to 323 s at the endpoint behind 4-6 minute task calls
(`max_concurrent_runs_per_endpoint` 1 gates model *calls*), then re-prefilled 47-65k tokens with
nothing cached. Going ahead of the queue (VOICE/TYPED) was already there; it cannot help when the
one slot is held by a call that runs for minutes.

Now interactive calls may use `interactive_reserved_calls` (1) slots background never takes, and
on llama.cpp every call is pinned to a slot of its class (`id_slot`, `cache_prompt`), chat in
0-1, background in 2-3. Verified on the live llama.cpp first: `id_slot` is honoured on
/v1/chat/completions (same 1.5k prompt again in the same slot: 1511 cached; in another: 0).

Throw-away alpha on ardi (:9123), production's roles and limit 1, the shared llama.cpp under
production load: a scheduled run writing a 1500-word essay, a chat question 12 s later, then a
second chat turn.

```
                          old (reserve 0, no pinning)      new (reserve 1, pinning)
chat 1  main call wait    602.7 s                          0.0 s
chat 1  main call         10.3 s, cached 0 / 8,337         2.3 s, cached 8,170 / 8,337
chat 1  first token       648.8 s                          ~78 s (see below)
chat 2  whole run         83.4 s                           4.1 s, cached 8,333 / 8,500
```

What is left of chat 1's 78 s is on the server, not in Jarvis: its 101-token classifier call got
its Jarvis slot at once and then sat 76 s in the llama.cpp fork's prefill admission
(`/slots` → `prefill_waiting: true`, `prefill_wait_ms` 44,362 seen for a tiny request while
another slot prefilled 58k tokens with nothing cached). With `-kvu` the 98k KV pool is shared by all four
slots, so a long task prompt still evicts the chat's even in its own slot; the server flags are
being changed separately.

## 2026-11-16 — the reserved slot overran the shared KV pool (core 2.0.0a128)

11:36, production: a chat call (49k prompt) let in by the reserved interactive slot beside a
scheduled call (65k prompt) asked vader's llama.cpp (`-c 98304 -np 4 -kvu`) for 114k of a 98,304
token pool. The fork answered `{'code': 500, 'message': 'Context size has been exceeded.'}`
mid-stream to both and both runs failed (model_calls 11:36:32 scheduled, 11:37:37 chat).
Stopgap on production: `interactive_reserved_calls` 0, which brings back the waits.

What the server says (GET, read-only): /props `total_slots` 4, `default_generation_settings.n_ctx`
98,304; /slots every slot `n_ctx` 98,304; /v1/models `meta.n_ctx` 98,304. There is no "unified"
flag; four slots of 98k on a 98k context is what `-kvu` looks like (without it each slot reports
its share, 24,576). So the pool is read off those numbers (`shared_kv_pool`).

Now `EndpointGate` admits a call by count AND, with a shared pool, by tokens: its estimated prompt
(the ledger's estimate, calibrated chars/token) plus `max_tokens` (16,384 when unset) must fit
beside the calls in flight; a call alone always goes. The order is unchanged - interactive first,
FIFO within a class, the first waiter that does not fit holds the ones behind it. Two 49k/65k
calls now run one after the other; two ordinary chat/classifier calls still run side by side.

If the pool runs out anyway (the estimate has no pictures and no chat-template overhead) and other
calls were in flight, the call waits for the endpoint to itself and goes again once: inside the
adapter when nothing had streamed yet, or as a step retried by the run loop
(`ModelError.crowded`, inspector shows "chat (again, alone)"). Alone, an overflow is real and
fails as before. vLLM manages its own KV (it queues what does not fit) and Ollama has one num_ctx
per loaded model, so both keep admission by count.
