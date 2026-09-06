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
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from jarvis_host import __version__
from jarvis_host.audio import Audio
from jarvis_host.com import ComWorker
from jarvis_host.config import HostConfig
from jarvis_host.files import Files
from jarvis_host.meetings import MeetingCapture
from jarvis_host.onenote import OneNoteBackend, OneNoteError, OneNoteService, onenote_dispatch
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
    onenote: OneNoteService | None
    files: Files
    shell: Shell
    screen: Screen
    audio: Audio
    status: Status
    meetings: MeetingCapture


def make_deps(
    config: HostConfig,
    *,
    dispatch: Callable[[], Any] | None = None,
    worker: ComWorker | None = None,
    onenote_dispatch_fn: Callable[[], Any] | None = None,
) -> Deps:
    worker = worker or ComWorker()
    backend = OutlookBackend(dispatch or outlook_dispatch, config.outlook_accounts)
    outlook = OutlookService(backend, worker)
    onenote = OneNoteService(OneNoteBackend(onenote_dispatch_fn or onenote_dispatch), worker)
    roots = config.fs_roots or ()
    files = Files(roots)
    shell = Shell(allowed=config.shell_allow, default_cwd=files.roots[0])
    screen = Screen(enabled=config.screen_enabled)
    return Deps(
        config,
        worker,
        outlook,
        onenote,
        files,
        shell,
        screen,
        Audio(),
        Status(config, worker, outlook),
        MeetingCapture(),
    )


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


def transport_security(config: HostConfig | None) -> TransportSecuritySettings:
    """DNS-rebinding protection for the MCP transport.

    The SDK trusts localhost only, so a core on another machine (ardi reaching this laptop over
    Tailscale) gets `421 Misdirected Request` until its address is listed. The guard defends
    browsers against rebinding; the daemon's own defence is the bearer token, so ``["*"]``
    (the default) turns it off rather than pretending a wildcard host list works — the SDK
    matches exact hosts and ``host:*`` port patterns only.
    """
    allowed = list(config.allowed_hosts) if config else ["*"]
    if "*" in allowed:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=allowed, allowed_origins=allowed
    )


def build_mcp(deps: Deps, config: HostConfig | None = None) -> FastMCP:
    mcp = FastMCP("jarvis-host", instructions=INSTRUCTIONS, transport_security=transport_security(config))

    def outlook() -> OutlookService:
        if deps.outlook is None:
            raise OutlookError("Outlook is only available on a Windows host")
        return deps.outlook

    def onenote() -> OneNoteService:
        if deps.onenote is None:
            raise OneNoteError("OneNote is only available on a Windows host")
        return deps.onenote

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
        account: str = "",
        folder: str = "inbox",
        since: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
        preview_chars: int = 400,
    ) -> str:
        """List messages in a folder, newest first, read in one GetTable pass. `since` = ISO-8601 lower bound on received time (a triage cursor). `cursor` continues a previous page (never skips or repeats). Each item carries sender, to/cc and a body `preview` of `preview_chars` (max 4000). Returns {items, cursor, total}; `cursor` is null when nothing remains. Item ids are session-scoped: use outlook_read/move/flag for the durable id."""
        return json_text(
            await _run(
                "outlook_list",
                lambda: outlook().call("list_items", account, folder, since, cursor, limit, preview_chars),
            )
        )

    @tool("outlook_read", READ)
    async def outlook_read(entry_id: str, account: str = "", max_chars: int = 20_000) -> str:
        """One message: headers, sender SMTP address, plain-text body (HTML converted), attachment names/sizes, flag and read state. `max_chars` caps the body (default 20k; use 3000-5000 for notification-style mail such as Jira, whose tail is boilerplate). Returns the durable entry_id."""
        cap = max(200, min(int(max_chars), 20_000))
        return json_text(await _run("outlook_read", lambda: outlook().call("read", entry_id, account, cap)))

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
    async def outlook_move(entry_id: str, folder: str, account: str = "", create: bool = False) -> str:
        """Move a message into `folder` (role, id or path) of its account. Re-resolves the id first; returns the message's new durable entry_id and the target folder path. `create=true` adds a missing subfolder at the end of an existing path (e.g. a new `Demands/DM-2300`); an unknown top-level folder is still an error."""
        return json_text(
            await _run("outlook_move", lambda: outlook().call("move", entry_id, folder, account, create))
        )

    @tool("outlook_folder_create", MUTATING_IDEMPOTENT)
    async def outlook_folder_create(path: str, account: str = "") -> str:
        """Create a folder path under the account's inbox (an existing top-level tree is reused, so are existing segments; safe to repeat). Returns the folder's path and id and which segments were created."""
        return json_text(
            await _run("outlook_folder_create", lambda: outlook().call("folder_create", path, account))
        )

    @tool("outlook_flag", MUTATING_IDEMPOTENT)
    async def outlook_flag(entry_id: str, flag: bool = True, account: str = "") -> str:
        """Flag (MarkAsTask) or unflag (ClearTaskFlag + FlagStatus=0) a message and verify the result via both FlagStatus and IsMarkedAsTask."""
        return json_text(await _run("outlook_flag", lambda: outlook().call("flag", entry_id, flag, account)))

    @tool("outlook_send", DESTRUCTIVE_OPEN)
    async def outlook_send(
        account: str,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        reply_to_entry_id: str = "",
        html: bool = False,
        draft: bool = False,
    ) -> str:
        """Send an email from `account`, or save it as a draft with `draft=true`. With `reply_to_entry_id` the message is built with Reply() so it stays in the thread — the inherited 'RE:' subject is kept and the body goes above the quoted original. Recipients may be comma- or semicolon-separated. The reply says whether it was sent or drafted."""
        return json_text(
            await _run(
                "outlook_send",
                lambda: outlook().call("send", account, to, subject, body, cc, reply_to_entry_id, html, draft),
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
        allow_overlap: bool = False,
    ) -> str:
        """Create an appointment (`start`/`end` ISO-8601 local; `end` defaults to +1h). REFUSES a slot that already holds a busy appointment - read calendar_list first and pick a free slot; pass allow_overlap=true only when overlapping is the intent. Attendees (comma-separated addresses) make it a meeting; invites go out only when send_invites is true."""
        return json_text(
            await _run(
                "calendar_create",
                lambda: outlook().call(
                    "calendar_create",
                    account,
                    subject,
                    start,
                    end,
                    location,
                    body,
                    attendees,
                    send_invites,
                    all_day,
                    allow_overlap,
                ),
            )
        )

    @tool("calendar_delete", DESTRUCTIVE)
    async def calendar_delete(entry_id: str, account: str = "") -> str:
        """Delete one appointment by the entry_id returned by calendar_list/calendar_create. Only appointments; returns what was deleted."""
        return json_text(await _run("calendar_delete", lambda: outlook().call("calendar_delete", entry_id, account)))

    @tool("calendar_invites", READ)
    async def calendar_invites(account: str = "", days: int = 14) -> str:
        """Meeting invites still unanswered in the next `days` days, one per organizer+subject: organizer address, slot, and the COMMITTED meetings that clash with it (other tentative or unanswered invites never count as clashes)."""
        return json_text(await _run("calendar_invites", lambda: outlook().call("calendar_invites", account, days)))

    @tool("calendar_respond", MUTATING)
    async def calendar_respond(entry_id: str, decision: str = "accept", comment: str = "", account: str = "") -> str:
        """Answer an invite and SEND the response to the organizer: decision accept | tentative | decline; `comment` is plain text put at the top of the response."""
        return json_text(
            await _run(
                "calendar_respond", lambda: outlook().call("calendar_respond", entry_id, decision, comment, account)
            )
        )

    @tool("calendar_free_slots", READ)
    async def calendar_free_slots(
        account: str = "",
        start: str | None = None,
        days: int = 5,
        duration_min: int = 30,
        work_start_hour: int = 9,
        work_end_hour: int = 18,
        limit: int = 3,
    ) -> str:
        """Up to `limit` free slots of `duration_min` minutes on weekdays inside the work hours, from `start` (ISO; default now) for `days` days, judged against COMMITTED meetings only."""
        return json_text(
            await _run(
                "calendar_free_slots",
                lambda: outlook().call(
                    "calendar_free_slots", account, start, days, duration_min, work_start_hour, work_end_hour, limit
                ),
            )
        )

    @tool("calendar_remove_canceled", DESTRUCTIVE)
    async def calendar_remove_canceled(account: str = "", days_back: int = 1, days_ahead: int = 60) -> str:
        """Delete meetings the organizer has CANCELLED from the calendar (they linger as crossed-out items). Nothing is sent; returns what was removed."""
        return json_text(
            await _run(
                "calendar_remove_canceled",
                lambda: outlook().call("calendar_remove_canceled", account, days_back, days_ahead),
            )
        )

    # --- OneNote -------------------------------------------------------------------------

    @tool("onenote_tree", READ)
    async def onenote_tree(with_pages: bool = True) -> str:
        """Notebooks → sections (section groups flattened) → pages, each with its id and its 'Notebook/Section/Page' path. Pass with_pages=false for a fast structure-only listing."""
        return json_text(await _run("onenote_tree", lambda: onenote().call("tree", with_pages)))

    @tool("onenote_read", READ)
    async def onenote_read(page: str, max_chars: int = 20_000) -> str:
        """Text of a OneNote page, by 'Notebook/Section/Page' path or by page id (from onenote_tree / onenote_search). Says when the text was truncated."""
        cap = max(500, min(int(max_chars or 20_000), 200_000))
        return json_text(await _run("onenote_read", lambda: onenote().call("read", page, cap)))

    @tool("onenote_search", READ)
    async def onenote_search(query: str, limit: int = 20) -> str:
        """Pages whose title or text matches `query`, through OneNote's own index. Returns ids and paths; read one with onenote_read."""
        return json_text(await _run("onenote_search", lambda: onenote().call("search", query, limit)))

    @tool("onenote_create", MUTATING)
    async def onenote_create(section: str, title: str, content: str = "") -> str:
        """Create a page in a section ('Notebook/Section' path from onenote_tree). `content` is Markdown: headings, bullets, numbered lists, quotes, fenced code and tables are converted; long content is written in chunks."""
        return json_text(await _run("onenote_create", lambda: onenote().call("create", section, title, content)))

    @tool("onenote_append", MUTATING)
    async def onenote_append(page: str, content: str) -> str:
        """Append Markdown to an existing page (path or id). Existing content is never rewritten - the new blocks are added to the page."""
        return json_text(await _run("onenote_append", lambda: onenote().call("append", page, content)))

    @tool("onenote_move", MUTATING)
    async def onenote_move(page: str, section: str) -> str:
        """Move a page into another section ('Notebook/Section' path). The page keeps its content and its id."""
        return json_text(await _run("onenote_move", lambda: onenote().call("move", page, section)))

    # --- meetings ------------------------------------------------------------------------

    @tool("meeting_start", MUTATING)
    async def meeting_start(meeting_id: str, sources: str = "mic,system") -> str:
        """Start capturing this machine's audio for a meeting: `sources` is 'mic', 'system' (what the speakers play) or both. Returns which sources opened and why any did not - one missing source does not stop the recording."""
        return json_text(await _run("meeting_start", lambda: asyncio.to_thread(deps.meetings.start, meeting_id, sources)))

    @tool("meeting_pull", READ)
    async def meeting_pull(meeting_id: str, after_seq: int = 0) -> str:
        """Audio captured since the last pull, as one 16 kHz mono WAV chunk (base64) with its offset from the start of the recording. Empty `chunks` means nothing new yet."""
        return json_text(
            await _run("meeting_pull", lambda: asyncio.to_thread(deps.meetings.pull, meeting_id, after_seq))
        )

    @tool("meeting_stop", MUTATING)
    async def meeting_stop(meeting_id: str) -> str:
        """Stop capturing and return the last chunk, so nothing recorded is lost between the final pull and the stop."""
        return json_text(await _run("meeting_stop", lambda: asyncio.to_thread(deps.meetings.stop, meeting_id)))

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

    @tool("volume_get", READ)
    async def volume_get() -> str:
        """Current system volume (0-100) and mute state of the default playback device."""
        return json_text(await _run("volume_get", lambda: deps.worker.acall(deps.audio.get, label="volume_get")))

    @tool("volume_set", MUTATING_IDEMPOTENT)
    async def volume_set(volume: int | None = None, muted: bool | None = None) -> str:
        """Set the system volume to an absolute percentage (0-100) and/or mute state, then read it back. Setting a volume above 0 unmutes. Use this instead of writing PowerShell: the media keys can only step the volume, so an absolute level cannot be reached by pressing them."""
        return json_text(
            await _run(
                "volume_set",
                lambda: deps.worker.acall(deps.audio.set, volume, muted, label="volume_set"),
            )
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
    mcp = build_mcp(deps, config)
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
