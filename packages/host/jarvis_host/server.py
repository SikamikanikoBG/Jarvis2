"""The MCP server: every tool, its annotations, and the bearer-token gate in front of it.

Annotations are the policy the core reads (``mcp_provider.py``): ``readOnlyHint=True`` on every
read; ``destructiveHint=True`` only on ``outlook_send``, ``calendar_create``, ``fs_write`` and
``shell_run``; ``outlook_move`` / ``outlook_flag`` are neither, i.e. mutating but reversible.
Results are JSON text (``screen_grab`` is the one image). Failures raise — the SDK marks the
result ``isError`` — so nothing here ever reports a success it did not observe.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from jarvis_host import __version__
from jarvis_host.com import ComWorker
from jarvis_host.config import HostConfig
from jarvis_host.files import Files
from jarvis_host.outlook import OutlookBackend, OutlookError, OutlookService, outlook_dispatch
from jarvis_host.screen import Screen
from jarvis_host.shell import Shell
from jarvis_host.status import Status

log = logging.getLogger("jarvis_host.tools")

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
MUTATING_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE_OPEN = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

INSTRUCTIONS = (
    "Local capabilities of one Windows machine: Outlook (COM), files under allowed roots, a shell, "
    "the screen. `account` is an Outlook store display name (empty = default store); `folder` is a "
    "well-known role (inbox, sent, drafts, deleted, junk), a folder id, or a '/'-separated path of "
    "localised names as returned by outlook_folders. Listings return short-lived ids — "
    "outlook_read/move/flag return the durable id. A non-null `cursor` means more items remain."
)


@dataclass
class Deps:
    config: HostConfig
    worker: ComWorker
    outlook: OutlookService | None
    files: Files
    shell: Shell
    screen: Screen
    status: Status


def make_deps(
    config: HostConfig, *, dispatch: Callable[[], Any] | None = None, worker: ComWorker | None = None
) -> Deps:
    worker = worker or ComWorker()
    backend = OutlookBackend(dispatch or outlook_dispatch, config.outlook_accounts)
    outlook = OutlookService(backend, worker)
    roots = config.fs_roots or ()
    files = Files(roots)
    shell = Shell(allowed=config.shell_allow, default_cwd=files.roots[0])
    screen = Screen(enabled=config.screen_enabled)
    return Deps(config, worker, outlook, files, shell, screen, Status(config, worker, outlook))


def json_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


async def _run(name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    """One log line per tool call: name, duration, ok/error."""
    t0 = time.monotonic()
    try:
        result = await fn()
    except Exception as exc:
        log.warning(
            "tool %-18s ERR %6.0fms %s: %s",
            name,
            (time.monotonic() - t0) * 1000,
            type(exc).__name__,
            str(exc).splitlines()[0] if str(exc) else "",
        )
        raise
    log.info("tool %-18s ok  %6.0fms", name, (time.monotonic() - t0) * 1000)
    return result


def build_mcp(deps: Deps) -> FastMCP:
    mcp = FastMCP("jarvis-host", instructions=INSTRUCTIONS)

    def outlook() -> OutlookService:
        if deps.outlook is None:
            raise OutlookError("Outlook is only available on a Windows host")
        return deps.outlook

    def tool(name: str, annotations: ToolAnnotations) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return mcp.tool(name=name, annotations=annotations, structured_output=False)

    # --- Outlook -------------------------------------------------------------------------

    @tool("outlook_accounts", READ)
    async def outlook_accounts() -> str:
        """Outlook stores (accounts) visible on this host, with their default folders (inbox, sent, drafts, deleted, junk, calendar, tasks — ids and localised names)."""
        return json_text(await _run("outlook_accounts", lambda: outlook().call("accounts")))

    @tool("outlook_folders", READ)
    async def outlook_folders(account: str = "") -> str:
        """Full folder tree of one account: id, name, path (localised, '/'-separated, usable as `folder`), kind, unread and total counts; default folders carry their role in `default`."""
        return json_text(await _run("outlook_folders", lambda: outlook().call("folders", account)))

    @tool("outlook_list", READ)
    async def outlook_list(
        account: str = "", folder: str = "inbox", since: str | None = None, cursor: str | None = None, limit: int = 25
    ) -> str:
        """List messages in a folder, newest first, read in one GetTable pass. `since` = ISO-8601 lower bound on received time (a triage cursor). `cursor` continues a previous page (never skips or repeats). Returns {items, cursor, total}; `cursor` is null when nothing remains. Item ids are session-scoped: use outlook_read/move/flag for the durable id."""
        return json_text(
            await _run("outlook_list", lambda: outlook().call("list_items", account, folder, since, cursor, limit))
        )

    @tool("outlook_read", READ)
    async def outlook_read(entry_id: str, account: str = "") -> str:
        """One message in full: headers, sender SMTP address, plain-text body (HTML converted; capped at 20k chars), attachment names/sizes, flag and read state. Returns the durable entry_id."""
        return json_text(await _run("outlook_read", lambda: outlook().call("read", entry_id, account)))

    @tool("outlook_search", READ)
    async def outlook_search(
        account: str = "",
        query: str = "",
        days_back: int = 30,
        cursor: str | None = None,
        limit: int = 25,
        folder: str = "inbox",
    ) -> str:
        """Find messages whose subject or sender contains `query` (case-insensitive) within the last `days_back` days of one folder. Every returned item verifiably matches; `cursor` pages further."""
        return json_text(
            await _run(
                "outlook_search", lambda: outlook().call("search", account, query, days_back, cursor, limit, folder)
            )
        )

    @tool("outlook_move", MUTATING)
    async def outlook_move(entry_id: str, folder: str, account: str = "") -> str:
        """Move a message into `folder` (role, id or path) of its account. Re-resolves the id first; returns the message's new durable entry_id and the target folder path."""
        return json_text(await _run("outlook_move", lambda: outlook().call("move", entry_id, folder, account)))

    @tool("outlook_flag", MUTATING_IDEMPOTENT)
    async def outlook_flag(entry_id: str, flag: bool = True, account: str = "") -> str:
        """Flag (MarkAsTask) or unflag (ClearTaskFlag + FlagStatus=0) a message and verify the result via both FlagStatus and IsMarkedAsTask."""
        return json_text(await _run("outlook_flag", lambda: outlook().call("flag", entry_id, flag, account)))

    @tool("outlook_send", DESTRUCTIVE_OPEN)
    async def outlook_send(
        account: str, to: str, subject: str, body: str, cc: str = "", reply_to_entry_id: str = "", html: bool = False
    ) -> str:
        """Send an email from `account`. With `reply_to_entry_id` the message is built with Reply() so it stays in the thread — the inherited 'RE:' subject is kept and the body goes above the quoted original. Recipients may be comma- or semicolon-separated."""
        return json_text(
            await _run(
                "outlook_send", lambda: outlook().call("send", account, to, subject, body, cc, reply_to_entry_id, html)
            )
        )

    @tool("calendar_list", READ)
    async def calendar_list(account: str = "", days: int = 7, start: str | None = None) -> str:
        """Appointments in the account's own calendar from `start` (ISO date/datetime; default today 00:00) for `days` days, recurrences expanded: subject, start/end, location, organizer, busy status, attendees."""
        return json_text(await _run("calendar_list", lambda: outlook().call("calendar_list", account, days, start)))

    @tool("calendar_create", DESTRUCTIVE)
    async def calendar_create(
        account: str,
        subject: str,
        start: str,
        end: str = "",
        location: str = "",
        body: str = "",
        attendees: str = "",
        send_invites: bool = False,
        all_day: bool = False,
    ) -> str:
        """Create an appointment (`start`/`end` ISO-8601 local; `end` defaults to +1h). Attendees (comma-separated addresses) make it a meeting; invites go out only when send_invites is true."""
        return json_text(
            await _run(
                "calendar_create",
                lambda: outlook().call(
                    "calendar_create", account, subject, start, end, location, body, attendees, send_invites, all_day
                ),
            )
        )

    # --- files ---------------------------------------------------------------------------

    @tool("fs_list", READ)
    async def fs_list(path: str) -> str:
        """Entries of a directory (name, type, size, modified) under the allowed roots; directories first."""
        return json_text(await _run("fs_list", lambda: asyncio.to_thread(deps.files.list, path)))

    @tool("fs_read", READ)
    async def fs_read(path: str, max_bytes: int = 256_000, offset: int = 0) -> str:
        """Text of a file under the allowed roots (UTF-8, up to 256 kB per call; use `offset` to continue when `truncated`). Binary files are refused."""
        return json_text(await _run("fs_read", lambda: asyncio.to_thread(deps.files.read, path, max_bytes, offset)))

    @tool("fs_write", DESTRUCTIVE)
    async def fs_write(path: str, text: str, append: bool = False) -> str:
        """Write (or append) UTF-8 text to a file under the allowed roots, creating parent directories."""
        return json_text(await _run("fs_write", lambda: asyncio.to_thread(deps.files.write, path, text, append)))

    @tool("fs_search", READ)
    async def fs_search(root: str, glob: str, limit: int = 500) -> str:
        """Files matching a glob (e.g. '*.pdf', '**/report*.xlsx') recursively under `root`, which must be inside the allowed roots."""
        return json_text(await _run("fs_search", lambda: asyncio.to_thread(deps.files.search, root, glob, limit)))

    # --- shell / screen / status ---------------------------------------------------------

    @tool("shell_run", DESTRUCTIVE)
    async def shell_run(command: str, cwd: str | None = None, timeout_s: int = 60) -> str:
        """Run a command in PowerShell (pwsh if installed) with a deadline; returns exit_code, stdout, stderr (each capped at 20k chars). A timeout is an error, not a result."""
        return json_text(
            await _run("shell_run", lambda: asyncio.to_thread(deps.shell.run, command, cwd, float(timeout_s)))
        )

    @tool("screen_grab", READ)
    async def screen_grab(monitor: int = 0) -> Image:
        """PNG screenshot: monitor 0 = all monitors as one image, 1.. = a single monitor."""
        png, _geometry = await _run("screen_grab", lambda: asyncio.to_thread(deps.screen.grab, monitor))
        return Image(data=png, format="png")

    @tool("host_status", READ)
    async def host_status() -> str:
        """This host: name, version, uptime, COM worker state (busy/pending/abandoned), Outlook connectivity and visible accounts, allowed roots, shell/screen switches."""
        return json_text(await _run("host_status", deps.status.snapshot))

    return mcp


class BearerAuth:
    """401 unless ``Authorization: Bearer <token>`` matches; ``/healthz`` stays open. Fails closed."""

    def __init__(self, app: ASGIApp, token: str, open_paths: frozenset[str] = frozenset({"/healthz"})) -> None:
        self.app = app
        self.token = token
        self.open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        header = ""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        expected = f"Bearer {self.token}"
        if not self.token or not hmac.compare_digest(header.encode("utf-8"), expected.encode("utf-8")):
            detail = "host has no token configured" if not self.token else "unauthorized"
            response: Response = JSONResponse(
                {"detail": detail}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(config: HostConfig, deps: Deps | None = None) -> BearerAuth:
    deps = deps or make_deps(config)
    mcp = build_mcp(deps)
    inner = mcp.streamable_http_app()  # creates the session manager; its lifespan is driven below

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        deps.worker.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            deps.worker.stop()

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True, "name": config.name, "version": __version__})

    app = Starlette(routes=[Route("/healthz", healthz), Mount("/", app=inner)], lifespan=lifespan)
    app.state.mcp = mcp
    app.state.deps = deps
    return BearerAuth(app, config.token)
