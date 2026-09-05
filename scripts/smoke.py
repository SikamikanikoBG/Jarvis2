"""End-to-end smoke test against a running core and a REAL model endpoint.

    uv run python scripts/smoke.py --base http://127.0.0.1:9020 --token "$JARVIS_TOKEN" \
        --provider vllm --url http://100.76.27.18:8010/v1 --model qwen3.8-27b

Exercises: settings PATCH, status probe, a plain streamed reply, a tool-calling reply
(jarvis.time), Stop mid-stream, and the persisted run events. Prints a compact report and
exits non-zero on the first failed check. No fakes: whatever it reports happened.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx
import websockets


def check(cond: bool, label: str, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        sys.exit(1)


async def ws_run(base: str, token: str | None, text: str, *, cancel_after_tokens: int | None = None) -> dict[str, Any]:
    url = base.replace("http", "ws", 1) + "/ws" + (f"?token={token}" if token else "")
    report: dict[str, Any] = {"deltas": 0, "reasoning": 0, "tools": [], "events": [], "text": "", "ttfb_ms": None}
    t0 = time.perf_counter()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "run.create", "text": text}))
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=300))
            report["events"].append(ev["type"])
            if ev["type"] == "run.queued":
                report["run_id"] = ev["run_id"]
                report["conversation_id"] = ev["conversation_id"]
            elif ev["type"] == "model.delta":
                if report["ttfb_ms"] is None:
                    report["ttfb_ms"] = int((time.perf_counter() - t0) * 1000)
                if ev["kind"] == "text":
                    report["deltas"] += 1
                    report["text"] += ev["text"]
                    if cancel_after_tokens and report["deltas"] >= cancel_after_tokens and "cancel_sent" not in report:
                        await ws.send(json.dumps({"type": "run.cancel", "run_id": report["run_id"]}))
                        report["cancel_sent"] = time.perf_counter()
                else:
                    report["reasoning"] += 1
            elif ev["type"] == "tool.call":
                report["tools"].append(ev["name"])
            elif ev["type"] == "model.done":
                report["usage"] = ev["usage"]
            elif ev["type"] in {"run.done", "run.failed", "run.cancelled"}:
                report["final"] = ev
                if "cancel_sent" in report:
                    report["cancel_latency_ms"] = int((time.perf_counter() - report["cancel_sent"]) * 1000)
                break
    return report


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9020")
    ap.add_argument("--token", default=None)
    ap.add_argument("--provider", choices=["ollama", "vllm"], required=True)
    ap.add_argument("--url", required=True, help="model endpoint base url")
    ap.add_argument("--model", required=True)
    ap.add_argument("--think", action="store_true", help="enable thinking for the chat role")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(base_url=args.base, headers=headers, timeout=60) as http:
        r = await http.get("/api/health")
        check(r.status_code == 200, "health", r.text[:80])

        settings = (await http.get("/api/settings")).json()
        roles = settings["roles"]
        for role in roles:
            roles[role] |= {"provider": args.provider, "base_url": args.url, "model": args.model}
            roles[role]["think"] = bool(args.think) if role == "chat" else False
        r = await http.patch("/api/settings", json={"roles": roles})
        check(r.status_code == 200, "settings patched to real endpoint", f"{args.provider} {args.model}")

        status = (await http.get("/api/status")).json()
        chat_ep = next(e for e in status["endpoints"] if e["role"] == "chat")
        check(chat_ep["ok"], "endpoint probe", f"{chat_ep['latency_ms']} ms, {chat_ep['detail'] or 'model served'}")

        # 1. plain reply
        rep = await ws_run(args.base, args.token, "Reply with exactly one short sentence: what can you help me with?")
        check(rep["final"]["type"] == "run.done", "plain reply completes", json.dumps(rep["final"])[:200])
        check(
            rep["deltas"] > 0 and rep["text"].strip() != "",
            "text streamed",
            f"{rep['deltas']} deltas, ttfb {rep['ttfb_ms']} ms",
        )
        u = rep.get("usage", {})
        print(
            f"      usage: prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} "
            f"ttft={u.get('ttft_ms')} ms dur={u.get('duration_ms')} ms reasoning_deltas={rep['reasoning']}"
        )
        print(f"      reply: {rep['text'].strip()[:200]!r}")

        # 2. tool call
        rep = await ws_run(
            args.base,
            args.token,
            "What is the exact current time in Sofia right now? Use your tool, then answer in one line.",
        )
        check(rep["final"]["type"] == "run.done", "tool run completes", json.dumps(rep["final"])[:200])
        check("jarvis.time" in rep["tools"], "model called jarvis.time", f"tools={rep['tools']}")
        print(f"      reply: {rep['text'].strip()[:200]!r}")
        events = (await http.get(f"/api/runs/{rep['run_id']}/events")).json()
        types = [e["type"] for e in events]
        check(
            "tool.call" in types and "tool.result" in types and "model.delta" not in types,
            "run events persisted without deltas",
            f"{len(events)} events",
        )

        # 3. stop mid-stream
        rep = await ws_run(
            args.base,
            args.token,
            "Write a long, detailed 800-word essay about the history of Sofia. Do not use tools.",
            cancel_after_tokens=8,
        )
        check(rep["final"]["type"] == "run.cancelled", "stop cancels the run", json.dumps(rep["final"])[:200])
        check(rep.get("cancel_latency_ms", 10**9) < 3000, "stop latency", f"{rep.get('cancel_latency_ms')} ms")
        msgs = (await http.get(f"/api/conversations/{rep['conversation_id']}/messages")).json()
        check(any(m.get("partial") for m in msgs), "partial text kept", f"{len(rep['text'])} chars streamed")

        print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
