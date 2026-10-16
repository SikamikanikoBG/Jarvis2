"""Read a call's trace and say what happened: the latest one, or the file given.

    uv run python check_trace.py [traces/....jsonl]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TRACES = Path(__file__).parent / "traces"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(TRACES.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    print(f"{path.name}: {len(rows)} events, {rows[-1]['t']:.0f} s")

    track = next((r for r in rows if r["kind"] == "client.track"), None)
    if track:
        s = track.get("settings", {})
        print(f"mic: echoCancellation={s.get('echoCancellation')} noiseSuppression={s.get('noiseSuppression')} "
              f"autoGainControl={s.get('autoGainControl')} ({track.get('label')})")
    for r in rows:
        if r["kind"] in ("client.route", "client.route_error", "client.devices"):
            if r["kind"] == "client.devices":
                outs = [d["label"] or d["id"] for d in r.get("devices", []) if d["kind"] == "audiooutput"]
                print(f"{r['t']:7.1f}  outputs ({r.get('reason')}): {outs}")
            else:
                print(f"{r['t']:7.1f}  {r['kind']}: {r.get('label') or r.get('error')} ({r.get('why')})")

    print("\nconversation:")
    hold_on = None
    for r in rows:
        k = r["kind"]
        if k == "user.transcript":
            print(f"{r['t']:7.1f}  ARSEN   {r['text']}")
        elif k == "bot.sentence":
            print(f"{r['t']:7.1f}  JARVIS  {r['text']}")
        elif k == "barge_in":
            print(f"{r['t']:7.1f}  -- barge-in: Jarvis cut off")
        elif k == "hold.on":
            hold_on = r["t"]
        elif k == "hold.off" and hold_on is not None:
            print(f"{r['t']:7.1f}  -- music {r['t'] - hold_on:.1f} s, stopped by: {r['why']}")
            hold_on = None
        elif k == "error":
            print(f"{r['t']:7.1f}  ERROR {r['error']}")

    levels_file = Path(__file__).parent / "test" / "levels.json"
    if levels_file.exists() and abs(levels_file.stat().st_mtime - path.stat().st_mtime) < 120:
        report_levels(rows, json.loads(levels_file.read_text()))

    stats = [r for r in rows if r["kind"] == "client.stats"]
    erle = [r.get("echo_return_loss_enhancement") for r in stats if r.get("echo_return_loss_enhancement") is not None]
    if erle:
        print(f"\nbrowser AEC echo return loss enhancement: min {min(erle):.1f} / max {max(erle):.1f} dB over {len(erle)} samples")
    counts = {k: sum(1 for r in rows if r["kind"] == k) for k in ("user.transcript", "bot.sentence", "barge_in", "hold.on", "error")}
    print("\ncounts:", counts)


def report_levels(rows: list[dict], levels: list[dict]) -> None:
    """Headless runs only: the received level (browser getStats) inside each music window and
    while Jarvis speaks, against the silence between them."""
    from datetime import datetime

    def epoch(r: dict) -> float:
        return datetime.fromisoformat(r["at"]).timestamp()

    def mean(a: float, b: float) -> float | None:
        vals = [x["level"] for x in levels if x["level"] is not None and a <= x["at"] / 1000 <= b]
        return sum(vals) / len(vals) if vals else None

    print("\nreceived level (headless getStats):")
    on = None
    for r in rows:
        if r["kind"] == "hold.on":
            on = epoch(r)
        elif r["kind"] == "hold.off" and on is not None:
            # skip the first 0.5 s of the fade-in; a window shorter than that is a blip
            if epoch(r) - on > 0.8:
                print(f"  music {r['t']:6.1f}s  level {mean(on + 0.5, epoch(r)) or 0:.4f}")
            on = None
    speak = [epoch(r) for r in rows if r["kind"] in ("bot.speaking_start", "bot.speaking_stop")]
    for a, b in zip(speak[::2], speak[1::2], strict=False):
        print(f"  voice {a - epoch(rows[0]):6.1f}s  level {mean(a, b) or 0:.4f}")
    quiet = [x["level"] for x in levels[:4] if x["level"] is not None]
    print(f"  (silence before the greeting: {quiet})")


if __name__ == "__main__":
    main()
