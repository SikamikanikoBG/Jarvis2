"""Fire one scheduled prompt and report exactly what happened, for validating a migration.

    uv run python scripts/run_schedule.py --token T --match "Wakeup sound"            # real fire
    uv run python scripts/run_schedule.py --token T --match "Burnout" --dry-run       # SAFE

--dry-run runs the schedule's prompt as an interactive *chat* run instead of a scheduled one.
Unattended runs never ask before a destructive tool; chat runs do - and this script answers
every confirmation with REJECT. So a dry run shows exactly which mails it would send, which
appointments it would create and which shell commands it would run, without doing any of it.
Lesson from 2026-09-05: a live Burnout run stacked 32 placeholder blocks onto a real calendar
before anyone looked. Dry-run first, always, for anything that writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def drive(
    token: str, *, run_id: str | None, conversation_id: str | None, prompt: str | None, timeout: float
) -> dict:
    """Watch a run to its end. With ``prompt`` set, create a chat run first and reject every
    confirmation it asks for (that is the dry run)."""
    import websockets

    url = f"{BASE.replace('http', 'ws', 1)}/ws?token={token}"
    report: dict = {"tools": [], "errors": [], "guards": [], "verdicts": [], "would_do": [], "text": "", "steps": 0}
    t0 = time.perf_counter()
    async with websockets.connect(url, max_size=None) as ws:
        if prompt is not None:
            await ws.send(json.dumps({"type": "run.create", "text": prompt, "kind": "chat"}))
        else:
            await ws.send(json.dumps({"type": "subscribe", "conversation_id": conversation_id}))
        while True:
            remaining = timeout - (time.perf_counter() - t0)
            if remaining <= 0:
                report["final"] = {"type": "TIMEOUT"}
                return report
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            t = ev["type"]
            if t == "run.queued" and prompt is not None and run_id is None:
                run_id = ev["run_id"]
                report["conversation_id"] = ev["conversation_id"]
            if ev.get("run_id") != run_id:
                continue
            if t == "tool.call":
                report["tools"].append(ev["name"])
            elif t == "tool.confirm_requested":
                args = json.dumps(ev["arguments"], ensure_ascii=False)
                report["would_do"].append(f"{ev['name']} {args[:220]}")
                await ws.send(
                    json.dumps(
                        {
                            "type": "tool.confirm",
                            "run_id": run_id,
                            "call_id": ev["call_id"],
                            "approved": False,
                            "note": "dry run - not executed",
                        }
                    )
                )
            elif t == "tool.result":
                if ev["result"]["kind"] == "error" and "rejected by user" not in ev["result"]["text"]:
                    report["errors"].append(f"{ev['name']}: {ev['result']['text'][:200]}")
            elif t == "model.delta" and ev["kind"] == "text":
                report["text"] += ev["text"]
            elif t == "model.done":
                report["steps"] += 1
                report["prompt_tokens"] = report.get("prompt_tokens", 0) + ev["usage"]["prompt_tokens"]
            elif t == "guard.armed":
                report["guards"].append(ev["detail"])
            elif t == "judge.verdict":
                report["verdicts"].append(f"{ev['verdict']}: {ev['reason'][:160]}")
            elif t in {"run.done", "run.failed", "run.cancelled"}:
                report["final"] = ev
                report["elapsed_s"] = round(time.perf_counter() - t0, 1)
                return report


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--match", required=True, help="substring of the schedule name")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--dry-run", action="store_true", help="run as chat and reject every destructive step")
    args = ap.parse_args()

    schedules = api("/api/schedules", args.token)
    hits = [s for s in schedules if args.match.lower() in s["name"].lower()]
    if len(hits) != 1:
        print(f"--match {args.match!r} matched {len(hits)} schedules: {[s['name'][:40] for s in hits]}")
        return 2
    s = hits[0]
    mode = "DRY RUN (destructive steps rejected)" if args.dry_run else "LIVE"
    print(f"=== {s['name'][:70]}  [{s['cron']}]  {mode}")
    if args.dry_run:
        rep = await drive(args.token, run_id=None, conversation_id=None, prompt=s["prompt"], timeout=args.timeout)
    else:
        fired = api(f"/api/schedules/{s['id']}/run", args.token, "POST", {})
        rep = await drive(
            args.token,
            run_id=fired["run_id"],
            conversation_id=fired["conversation_id"],
            prompt=None,
            timeout=args.timeout,
        )
        rep["conversation_id"] = fired["conversation_id"]

    final = rep.get("final", {})
    print(
        f"    result   : {final.get('type')}  in {rep.get('elapsed_s')}s, {rep['steps']} model calls, {rep.get('prompt_tokens', 0):,} prompt tokens"
    )
    print(f"    tools    : {rep['tools'] or 'NONE - it only produced prose'}")
    if rep["would_do"]:
        print(f"    WOULD DO ({len(rep['would_do'])} destructive steps, all rejected):")
        for w in rep["would_do"]:
            print(f"       - {w}")
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
    print(f"    reply    : {' '.join(rep['text'].split())[:700]}")
    print(f"    conv     : {rep.get('conversation_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
