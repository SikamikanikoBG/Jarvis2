"""Call ONE tool on a running ``jarvis-host`` and print its result.

    uv run python scripts/host_call.py outlook_folders '{"account": ""}'
    uv run python scripts/host_call.py calendar_list '{"days": 7}' [--url …] [--token …]

Read-only by design: mutating tools (send, move, flag, create, delete, write, shell, volume)
are refused unless ``--allow-mutate`` is given explicitly. The token comes from
``%LOCALAPPDATA%\\Jarvis2\\host.toml`` unless ``--token`` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "host"))
from jarvis_host.config import default_config_path, parse_config

MUTATING = {
    "outlook_send",
    "outlook_move",
    "outlook_flag",
    "calendar_create",
    "calendar_delete",
    "fs_write",
    "shell_run",
    "volume_set",
}


async def main() -> int:
    # Windows consoles default to a legacy code page; mail previews carry emoji.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("tool")
    ap.add_argument("args", nargs="?", default="{}", help="JSON object of tool arguments")
    ap.add_argument("--url", default="http://127.0.0.1:9030/mcp")
    ap.add_argument("--token")
    ap.add_argument("--allow-mutate", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ns = ap.parse_args()

    if ns.tool in MUTATING and not ns.allow_mutate:
        print(f"refusing mutating tool {ns.tool!r} without --allow-mutate", file=sys.stderr)
        return 2
    token = ns.token
    if not token:
        path = default_config_path()
        token = parse_config(path.read_text(encoding="utf-8"), path).token
    arguments = json.loads(ns.args)

    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30, read=ns.timeout)) as http,
        streamable_http_client(ns.url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        res = await session.call_tool(ns.tool, arguments, read_timeout_seconds=timedelta(seconds=ns.timeout))
        text = "".join(getattr(c, "text", "") for c in res.content)
        if res.isError:
            print(f"ERROR: {text}", file=sys.stderr)
            return 1
        try:
            print(json.dumps(json.loads(text), ensure_ascii=False, indent=1))
        except ValueError:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
