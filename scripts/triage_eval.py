"""Check a triage category set against the REAL mailbox without moving anything.

    uv run python scripts/triage_eval.py --token T [--base http://100.97.120.53:9020]
        [--settings triage_settings.json] [--sample "Action Hub/To-Do=20" --sample "Jira=10" ...]

1. optionally PATCHes ``settings.triage`` from a JSON file (``{"triage": {...}}``),
2. dry-runs the inbox (what the next live pass would do),
3. dry-runs samples of already-sorted folders and reports how often the proposal agrees with
   where the mail actually lives - the accuracy number for the category set, mail by mail.

Nothing is moved, recorded or advanced: the core refuses a folder sample outside a dry run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import httpx

DEFAULT_SAMPLES = [
    "Action Hub/To-Do=20",
    "Action Hub/Reference=20",
    "Smart Lab/AI=15",
    "Smart Lab/RPA=10",
    "Leadership/Bosses=15",
    "Jira=10",
    "Quarantine=10",
]


def norm(path: str) -> str:
    """Compare folders by their inbox-relative tail: 'Входящи/Action Hub/To-Do' == 'Action Hub/To-Do'."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if parts and parts[0].lower() in {"inbox", "входящи"}:
        parts = parts[1:]
    return "/".join(parts).lower()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]  # Cyrillic subjects
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://100.97.120.53:9020")
    ap.add_argument("--token", required=True)
    ap.add_argument("--settings", help="JSON file with a top-level 'triage' block to PATCH first")
    ap.add_argument("--sample", action="append", help="FOLDER=N (repeatable); default = the V1 folder set")
    ap.add_argument("--no-inbox", action="store_true")
    ap.add_argument("--account", help="only this mailbox (folders differ between work and a personal one)")
    args = ap.parse_args()
    scope = {"account": args.account} if args.account else {}

    client = httpx.Client(
        base_url=args.base, headers={"Authorization": f"Bearer {args.token}"}, timeout=httpx.Timeout(30, read=1500)
    )
    if args.settings:
        body = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        r = client.patch("/api/settings", json=body)
        r.raise_for_status()
        t = r.json()["triage"]
        print(f"settings: triage host={t['host']} accounts={t['accounts']} categories={len(t['categories'])} enabled={t['enabled']}")

    if not args.no_inbox:
        r = client.post("/api/triage/run", params={"dry_run": "true", **scope})
        r.raise_for_status()
        rep = r.json()
        print(f"\n=== INBOX dry run: {rep['processed']} mails, {rep['routed']} would move, errors={rep['errors']}")
        for p in rep["proposed"]:
            print(f"   {p['folder']:<28} <- [{p['sender'][:22]:<22}] {p['subject']}")

    total_ok = total_n = 0
    for spec in args.sample or DEFAULT_SAMPLES:
        folder, _, n = spec.partition("=")
        r = client.post("/api/triage/run", params={"dry_run": "true", "folder": folder, "limit": int(n or 20), **scope})
        if r.status_code != 200:
            print(f"\n=== {folder}: HTTP {r.status_code} {r.text[:200]}")
            continue
        rep = r.json()
        if rep["errors"]:
            print(f"\n=== {folder}: errors {rep['errors']}")
            continue
        ok = sum(1 for p in rep["proposed"] if norm(p["folder"]) == norm(folder))
        n_items = len(rep["proposed"])
        total_ok += ok
        total_n += n_items
        dist = Counter(p["folder"] for p in rep["proposed"])
        pct = f"{100 * ok / n_items:.0f}%" if n_items else "n/a"
        print(f"\n=== {folder}: {ok}/{n_items} agree ({pct}); proposed: {dict(dist)}")
        for p in rep["proposed"]:
            if norm(p["folder"]) != norm(folder):
                print(f"   DIFF -> {p['folder']:<28} [{p['sender'][:22]:<22}] {p['subject']}")
    if total_n:
        print(f"\nOVERALL agreement with the folders V1 chose: {total_ok}/{total_n} = {100 * total_ok / total_n:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
