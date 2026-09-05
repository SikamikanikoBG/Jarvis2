"""Measure whether the prompt prefix is being cached: three turns in ONE conversation.

    uv run python scripts/cache_probe.py --token T [--base http://100.97.120.53:9020]

Turn 1 is a cold prefill. If the prefix is stable, turns 2 and 3 should show a TTFT far below
turn 1 for a similar prompt size (vLLM prefix caching). If TTFT grows with the prompt every
turn, something in the prompt before the new input is changing (2026-09-05: the KG/skills
blocks in the system prompt did exactly that - 18 s TTFT on 34k tokens).
"""

from __future__ import annotations

import argparse
import asyncio
import json


async def turn(ws, conv_id: str | None, text: str) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
    payload: dict = {"type": "run.create", "text": text}
    if conv_id:
        payload["conversation_id"] = conv_id
    await ws.send(json.dumps(payload))
    out: dict = {"calls": []}
    while True:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=900))
        if ev["type"] == "run.queued" and "run" not in out:
            out["run"] = ev["run_id"]
            conv_id = ev["conversation_id"]
        if ev.get("run_id") != out.get("run"):
            continue
        if ev["type"] == "model.done":
            u = ev["usage"]
            out["calls"].append((u["prompt_tokens"], u["ttft_ms"], u["duration_ms"]))
        if ev["type"] in ("run.done", "run.failed", "run.cancelled"):
            out["final"] = ev["type"]
            return conv_id or "", out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://100.97.120.53:9020")
    ap.add_argument("--token", required=True)
    args = ap.parse_args()
    import websockets

    url = f"{args.base.replace('http', 'ws', 1)}/ws?token={args.token}"
    turns = [
        "Прочети ми кратко какво има в календара за понеделник и вторник. Не пиши повече от 6 реда.",
        "А сряда?",
        "Благодаря. Кой ден е най-натоварен от трите?",
    ]
    async with websockets.connect(url, max_size=None) as ws:
        conv = None
        for i, text in enumerate(turns, 1):
            conv, r = await turn(ws, conv, text)
            if i == 1:
                await ws.send(json.dumps({"type": "subscribe", "conversation_id": conv}))
            for j, (p, ttft, dur) in enumerate(r["calls"], 1):
                cold = " (cold)" if i == 1 and j == 1 else ""
                print(f"  turn {i} call {j}{cold}: prompt {p:>6,} tok | TTFT {ttft:>6} ms | total {dur:>6} ms | {r['final']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
