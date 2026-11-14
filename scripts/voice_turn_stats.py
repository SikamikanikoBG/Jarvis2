"""How often a call turn was sent before Arsen had finished: read-only, from the core's database.

    python voice_turn_stats.py --db /data/jarvis2.db [--since 2026-10-25] [--until 2026-11-15]

On ardi, inside the core's container (the database is opened read-only, nothing is written;
ssh there does not pass stdin, so the script goes as an argument):

    B64=$(base64 -w0 scripts/voice_turn_stats.py)
    ssh ardi@100.97.120.53 "docker exec jarvis2-core /app/.venv/bin/python -c \\
        \\"import base64,sys;sys.argv=['s','--db','/data/jarvis2.db'];exec(base64.b64decode('$B64'))\\""

A voice turn is a run with channel 'voice'. It was split when more of his words reached it as a
steer (a user message on the run after its first) within --within seconds of the run being
created: he was still talking when it went (2026-11-14, "при малка пауза мисли че съм готов").
A steer that starts with the cut-in note is him talking over the answer, not a split, and is not
counted. Also: any steer at all, the calls with at least one split, and the median words in what
he said per turn (a briefing that arrives whole is long). The target after the call-patience
change (issue #12) is under 5 % split within 20 s.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime

#: The note the web puts in front of a cut-in's words (web/src/voice/session.ts, CUT_NOTE).
CUT_IN = "[The user cut you off"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--db", required=True, help="path to jarvis2.db (opened read-only)")
    ap.add_argument("--since", default="2026-10-25", help="first day, UTC (YYYY-MM-DD)")
    ap.add_argument("--until", default="2026-11-15", help="day after the last, UTC (YYYY-MM-DD)")
    ap.add_argument("--within", type=float, default=20.0, help="seconds after the run a steer counts as a split")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    runs = db.execute(
        "SELECT id, created_at, call_id FROM runs WHERE channel = 'voice' AND created_at >= ? AND created_at < ?"
        " ORDER BY created_at",
        (args.since, args.until),
    ).fetchall()
    split = steered = 0
    words: list[int] = []
    calls: dict[str, bool] = defaultdict(bool)
    for run_id, created, call_id in runs:
        said = db.execute(
            "SELECT created_at, content FROM messages WHERE run_id = ? AND role = 'user' AND name IS NULL"
            " ORDER BY created_at",
            (run_id,),
        ).fetchall()
        more = [(at, text) for at, text in said[1:] if not text.startswith(CUT_IN)]
        start = datetime.fromisoformat(created)
        early = any((datetime.fromisoformat(at) - start).total_seconds() <= args.within for at, _ in more)
        steered += bool(more)
        split += early
        calls[call_id or run_id] |= early
        words.append(sum(len(text.split()) for _, text in said if not text.startswith(CUT_IN)))

    n = len(runs)
    pct = (lambda k: f"{100 * k / n:.1f} %") if n else (lambda k: "-")
    print(f"voice turns {args.since} .. {args.until} (UTC): {n}")
    print(f"  split (more words within {args.within:g} s): {split} ({pct(split)})")
    print(f"  any steer that was not a cut-in:        {steered} ({pct(steered)})")
    print(f"  calls with a split: {sum(calls.values())} of {len(calls)}")
    print(f"  median words per turn: {statistics.median(words) if words else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
