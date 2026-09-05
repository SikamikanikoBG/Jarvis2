"""Fire one scheduled prompt and report exactly what happened, for validating a migration.

    uv run python scripts/run_schedule.py --match "Wakeup sound" [--timeout 900]

Prints the tools it called (with failures marked), the supervisor's verdicts, token/step cost
and the final reply, so a schedule that "worked" can be told apart from one that quietly
produced prose instead of doing the job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request

BASE = "http://100.97.120.53:9020"


def api(path: str, token: str, method: str = "GET", body: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


async def watch(token: str, run_id: str, conversation_id: str, timeout: float) -> dict:
    import websockets

    url = f"{BASE.replace('http', 'ws', 1)}/ws?token={token}"
    report: dict = {"tools": [], "errors": [], "guards": [], "verdicts": [], "text": "", "steps": 0}
    t0 = time.perf_counter()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "subscribe", "conversation_id": conversation_id}))
        while True:
            remaining = timeout - (time.perf_counter() - t0)
            if remaining <= 0:
                report["final"] = {"type": "TIMEOUT"}
                return report
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            if ev.get("run_id") != run_id:
                continue
            t = ev["type"]
            if t == "tool.call":
                report["tools"].append(ev["name"])
            elif t == "tool.result":
                if ev["result"]["kind"] == "error":
                    report["errors"].append(f"{ev['name']}: {ev['result']['text'][:200]}")
            elif t == "model.delta" and ev["kind"] == "text":
                report["text"] += ev["text"]
            elif t == "model.done":
                report["steps"] += 1
            elif t == "guard.armed":
                report["guards"].append(ev["detail"])
            elif t == "judge.verdict":
                report["verdicts"].append(f"{ev['verdict']}: {ev['reason'][:160]}")
            elif t in {"run.done", "run.failed", "run.cancelled", "run.waiting_user"}:
                report["final"] = ev
                report["elapsed_s"] = round(time.perf_counter() - t0, 1)
                return report


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--match", required=True, help="substring of the schedule name")
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    schedules = api("/api/schedules", args.token)
    hits = [s for s in schedules if args.match.lower() in s["name"].lower()]
    if len(hits) != 1:
        print(f"--match {args.match!r} matched {len(hits)} schedules: {[s['name'][:40] for s in hits]}")
        return 2
    s = hits[0]
    print(f"=== {s['name'][:70]}  [{s['cron']}]")
    fired = api(f"/api/schedules/{s['id']}/run", args.token, "POST", {})
    rep = await watch(args.token, fired["run_id"], fired["conversation_id"], args.timeout)

    final = rep.get("final", {})
    print(f"    result   : {final.get('type')}  in {rep.get('elapsed_s')}s, {rep['steps']} model calls")
    print(f"    tools    : {rep['tools'] or 'NONE - it only produced prose'}")
    if rep["errors"]:
        print("    FAILURES :")
        for e in rep["errors"]:
            print(f"       - {e}")
    for g in rep["guards"]:
        print(f"    guard    : {g}")
    for v in rep["verdicts"]:
        print(f"    judge    : {v}")
    if final.get("summary"):
        print(f"    summary  : {final['summary'][:200]}")
    print(f"    reply    : {' '.join(rep['text'].split())[:600]}")
    print(f"    conv     : {fired['conversation_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
