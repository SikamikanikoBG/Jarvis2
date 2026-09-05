# Phase 1 — Harness + chat: user stories

Format: story → acceptance criteria (each one is a test). "Arsen" is the single user.

## H1. Chat with a local model from any device

As Arsen, I open the web UI on my phone, type a message, and see the answer stream in.

- Sending creates a `chat` run in the current conversation; the composer disables until
  `run.done|failed|cancelled`.
- Text deltas render as they arrive; reasoning deltas render in a collapsed fold that shows a
  live "thinking…" indicator and the elapsed time.
- The reply is persisted with its reasoning; reloading the page shows the same transcript.
- First delta reaches the browser ≤ 100 ms after the model emits it (fake model in CI).

## H2. Stop means stop

As Arsen, when I press Stop, the model stops within a second and nothing keeps running.

- `run.cancel` closes the model stream and cancels an in-flight tool call.
- The UI shows "Stopped" only when `run.cancelled` arrives, never optimistically.
- A cancelled run's partial text is kept and marked as partial.
- Test: cancel during stream; cancel during a 10 s fake tool; cancel while queued.

## H3. A run survives a core restart

As Arsen, if the core restarts mid-task, the task continues; I do not lose the plan or the
work so far.

- On boot, `running` runs become `interrupted`, then `run.resumed` fires and the loop continues
  from the last completed boundary.
- If the interrupted step was a **read-only** tool, it is re-issued. If it was a **mutating**
  tool, the run enters `waiting_user` with a card asking whether the action happened.
- Test: kill after model call, before tool result → resumes; kill during mutating tool → waits.

## H4. Tools are called safely and visibly

As Arsen, I see every tool call as a card (name, args, duration, result kind) and nothing is
executed twice.

- Arguments are validated against the tool schema before dispatch; invalid → `tool.result`
  with `kind=error` returned to the model, once.
- Each mutating call carries an idempotency key; a replay is refused by the provider.
- `tool.call`/`tool.result` events include `seq`, duration, `kind`, `count/total` when paged.

## H5. The run cannot spiral

As Arsen, a stuck or looping run ends by itself with an explanation, never by silence.

- Budgets per kind (chat: 25 steps / 10 min): exhaustion → `run.done` with a summary message
  saying what was and was not finished.
- Repeated identical tool call (same name + args, 3×) → `guard.armed` → judge call → verdict
  event → engine enforces (`stop` ends the run with the judge's reason as the last message).
- Every guard emitted in the codebase is consumed in at least one scenario test.

## H6. Conversations, folders, and the sidebar

As Arsen, my sidebar shows chats flat and everything else grouped so background work never
floods it.

- Sidebar: `Chats` (flat, newest first), then collapsible folders per `kind` and `folder_key`.
- New/rename/archive/delete conversation; switching conversations subscribes the WS to it.
- Unread badge on a conversation that received `run.done` while not open.

## H7. Run Inspector

As Arsen, I can open any run and see exactly what happened, step by step, with timings and
token usage.

- Timeline of `run_events` with type, timestamp, duration since previous, payload preview.
- Model calls show prompt tokens, completion tokens, TTFT, tok/s; tool calls show duration.
- Works for a running run (live) and a finished one.

## H8. Model endpoints are configured per role and reported honestly

As Arsen, I set which model serves which role, and the status screen tells me what is
actually reachable.

- Settings: roles `chat`, `planner`, `classifier`, `judge`, `triage` → `{provider, base_url,
  model, think, num_ctx, temperature, max_tokens}`; only `chat` may enable `think`.
- Status: each configured endpoint probed (`/api/tags` or `/v1/models`) with latency; a
  failing endpoint is red — there is no automatic fallback to another provider.

## H9. It is fast

- Context assembly for a 50-message conversation < 150 ms (measured in a test).
- WS event fan-out to 3 subscribers < 20 ms per event (measured in a test).
- No thread other than the asyncio loop and aiosqlite's connection worker.

## Definition of done (Phase 1)

CI green (ruff, pyright strict, pytest, vitest, playwright headless with the fake model).
Real gate: chat with `qwen3.8-27b` on vader from the phone; Stop stops; `docker restart`
mid-run and the run resumes.
