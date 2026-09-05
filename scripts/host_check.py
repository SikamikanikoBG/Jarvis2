"""Read-only smoke check of a running ``jarvis-host`` through the MCP client.

    uv run python scripts/host_check.py [--url http://127.0.0.1:9030/mcp] [--token …] [--full]

Lists the tools with their annotations, then exercises ONLY read tools against the live
machine: outlook_accounts, outlook_folders, outlook_list (+ a second page and a `since`
window), outlook_read, calendar_list, fs_list, host_status. Mutating tools are refused by the
script itself. Subjects and addresses are redacted to three characters unless ``--full``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "host"))
from jarvis_host.config import default_config_path, parse_config

FORBIDDEN = {"outlook_send", "outlook_move", "outlook_flag", "calendar_create", "fs_write", "shell_run"}
REDACT = True


def redact(value: Any) -> Any:
    if not REDACT:
        return value
    if isinstance(value, str):
        return value if len(value) <= 3 else value[:3] + "…"
    return value


def redact_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    if "subject" in out:
        out["subject"] = redact(out["subject"])
    if isinstance(out.get("from"), dict):
        out["from"] = {k: redact(v) for k, v in out["from"].items()}
    for key in ("to", "cc", "organizer", "location"):
        if key in out:
            out[key] = redact(out[key])
    if "attendees" in out:
        out["attendees"] = [redact(a) for a in out["attendees"]]
    if "entry_id" in out and isinstance(out["entry_id"], str):
        out["entry_id"] = out["entry_id"][:12] + f"…({len(out['entry_id'])})"
    return out


class Check:
    def __init__(self, session: ClientSession) -> None:
        self.session = session
        self.rows: list[tuple[str, float, str]] = []

    async def call(self, name: str, **args: Any) -> Any:
        if name in FORBIDDEN:
            raise RuntimeError(f"refusing to call {name}: host_check is read-only")
        t0 = time.monotonic()
        try:
            res = await self.session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=120))
        except Exception as exc:
            self.rows.append((name, (time.monotonic() - t0) * 1000, f"EXC {type(exc).__name__}: {exc}"))
            raise
        ms = (time.monotonic() - t0) * 1000
        text = "".join(getattr(c, "text", "") for c in res.content)
        if res.isError:
            self.rows.append((name, ms, f"ERROR {text[:200]}"))
            raise RuntimeError(f"{name}: {text}")
        self.rows.append((name, ms, "ok"))
        return json.loads(text) if text.strip().startswith(("{", "[")) else text


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:9030/mcp")
    parser.add_argument("--token", default=None, help="bearer token (default: read from host.toml)")
    parser.add_argument("--account", default="", help="account to exercise (default: the first one listed)")
    parser.add_argument("--full", action="store_true", help="do not redact subjects/addresses")
    parser.add_argument(
        "--skip-outlook", action="store_true", help="only host_status + fs_list (e.g. while Outlook is hung)"
    )
    args = parser.parse_args()
    global REDACT  # noqa: PLW0603
    REDACT = not args.full

    token = args.token
    if not token:
        path = default_config_path()
        if not path.exists():
            print(f"no --token and no config at {path}", file=sys.stderr)
            return 2
        token = parse_config(path.read_text(encoding="utf-8"), path).token
        print(f"token: from {path}")

    failures = 0
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30, read=180)) as http,
        streamable_http_client(args.url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        print(f"connected: {init.serverInfo.name} {init.serverInfo.version or ''} at {args.url}")
        tools = (await session.list_tools()).tools
        print(f"\n{len(tools)} tools:")
        print(f"  {'name':<18} {'readOnly':<9} {'destructive':<12} {'idempotent':<11} {'openWorld':<9}")
        for t in tools:
            a = t.annotations
            flags = [a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint] if a else [None] * 4
            print(f"  {t.name:<18} " + " ".join(f"{f!s:<{w}}" for f, w in zip(flags, (9, 12, 11, 9), strict=True)))

        chk = Check(session)
        print("\nread-only calls:")
        skip_outlook = bool(args.skip_outlook)

        async def step(label: str, coro: Any) -> Any:
            nonlocal failures
            if skip_outlook and label.startswith(("outlook", "calendar")):
                coro.close()
                print(f"  - {label}: skipped")
                return None
            try:
                return await coro
            except Exception as exc:
                failures += 1
                print(f"  ! {label}: {str(exc)[:300]}")
                return None

        # host_status first: it never queues behind a stuck COM call and says whether Outlook is hung,
        # so a frozen Outlook costs one line here instead of four minutes of timeouts below.
        status = await step("host_status", chk.call("host_status"))
        if status:
            com = status["com"]
            ol = status["outlook"]
            print(
                f"  status: {status['name']} v{status['version']} up {status['uptime_s']}s; com busy={com['busy']} pending={com['pending']} abandoned={com['abandoned']} completed={com['completed']}; outlook connected={ol.get('connected')} stores={ol.get('stores')}"
            )
            proc = ol.get("process") or {}
            if proc:
                print(
                    f"  outlook process: running={proc.get('running')} hung={proc.get('hung')} windows={[redact(p.get('window', '')) for p in proc.get('processes', [])]}"
                )
            if ol.get("diagnosis"):
                print(f"  ! {ol['diagnosis']}")
            if proc.get("hung") and not skip_outlook:
                skip_outlook = True
                print("  (Outlook is hung: skipping the Outlook calls; restart Outlook and rerun)")

        accounts = await step("outlook_accounts", chk.call("outlook_accounts"))
        account = args.account
        if accounts:
            print(f"  accounts: {len(accounts)}")
            for a in accounts:
                roles = {k: v["name"] for k, v in a.get("folders", {}).items()}
                print(f"    - {redact(a['name'])} type={a['type']} default={a['default']} folders={roles}")
            account = account or accounts[0]["name"]

        folders = await step("outlook_folders", chk.call("outlook_folders", account=account))
        if folders:
            inbox = next((f for f in folders["folders"] if f.get("default") == "inbox"), None)
            print(
                f"  folders: {len(folders['folders'])} in {redact(folders['account'])}; inbox = {inbox['path'] if inbox else '?'} (unread {inbox['unread'] if inbox else '?'}, total {inbox.get('total') if inbox else '?'})"
            )

        page = await step("outlook_list", chk.call("outlook_list", account=account, folder="inbox", limit=5))
        first_id = None
        if page:
            print(
                f"  list: {len(page['items'])} items of total {page.get('total')} in {page['folder']}; cursor={'yes' if page['cursor'] else 'no'}"
            )
            for it in page["items"]:
                r = redact_item(it)
                print(
                    f"    - {r['received']} {'U' if it['unread'] else ' '}{'F' if it['flagged'] else ' '} {r['from']['address']:<14} {r['subject']} [{it['kind']}]"
                )
            first_id = page["items"][0]["entry_id"] if page["items"] else None
            if page["cursor"]:
                page2 = await step(
                    "outlook_list(cursor)",
                    chk.call("outlook_list", account=account, folder="inbox", cursor=page["cursor"], limit=5),
                )
                if page2:
                    ids1 = {i["entry_id"] for i in page["items"]}
                    overlap = [i for i in page2["items"] if i["entry_id"] in ids1]
                    print(
                        f"  list page 2: {len(page2['items'])} items, overlap with page 1: {len(overlap)}, cursor={'yes' if page2['cursor'] else 'no'}"
                    )
                    if page2["items"]:
                        print(
                            f"    first: {redact_item(page2['items'][0])['received']} {redact_item(page2['items'][0])['subject']}"
                        )
            if len(page["items"]) >= 3 and page["items"][2]["received"]:
                since = page["items"][2]["received"]
                windowed = await step(
                    "outlook_list(since)",
                    chk.call("outlook_list", account=account, folder="inbox", since=since, limit=50),
                )
                if windowed:
                    print(
                        f"  list since {since}: {len(windowed['items'])} items (expected 3), total={windowed.get('total')}"
                    )

        if first_id:
            msg = await step("outlook_read", chk.call("outlook_read", entry_id=first_id, account=account))
            if msg:
                r = redact_item(msg)
                print(
                    f"  read: id {r['entry_id']} subject={r['subject']} from={r['from']} body={len(msg['body'])} chars{' (truncated)' if msg['body_truncated'] else ''} attachments={[redact(a['name']) for a in msg['attachments']]} flagged={msg['flagged']} is_task={msg['is_task']}"
                )

        cal = await step("calendar_list", chk.call("calendar_list", account=account, days=7))
        if cal:
            print(
                f"  calendar: {len(cal['events'])} events {cal['from'][:10]}..{cal['to'][:10]} in {redact(cal['calendar'])} (checked {cal['items_checked']}, via {cal['source']})"
            )
            for ev in cal["events"][:5]:
                r = redact_item(ev)
                print(
                    f"    - {r['start']} → {(r['end'] or '')[11:16]} {r['subject']} @{r['location']} [{ev['busy']}{', recurring' if ev['recurring'] else ''}]"
                )

        home = str(Path.home())
        fs = await step("fs_list", chk.call("fs_list", path=home))
        if fs:
            print(f"  fs_list {home}: {len(fs['entries'])} entries{' (truncated)' if fs['truncated'] else ''}")

        print("\ntimings:")
        for name, ms, outcome in chk.rows:
            print(f"  {name:<22} {ms:8.0f} ms  {outcome}")
        total = sum(ms for _, ms, _ in chk.rows)
        print(f"  {'total':<22} {total:8.0f} ms  {len(chk.rows)} calls, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
