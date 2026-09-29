"""The browser extensions as ONE tool provider over the WebSocket (namespace ``browser``).

Every call carries the **session** it is made from — the conversation id, which the engine puts
in a ContextVar for the run (``engine/current.py``). The extension keeps one work tab per
session, so two chats browsing at the same time no longer drive the same tab: before this,
whichever ran second read whatever the first had just opened (2026-09-20, "they are competing
for the same tab"). When a run ends, ``job_done`` tells the extension that session's job is over
and the tab can go.

Several browsers can be connected at once (2026-09-29: the laptop's Brave was refused with 4409
for as long as the VM's Brave held the one slot, and the extension could only say "/ws did not
open"). Each connection has a NAME: the one its hello gives, else the MCP host at the same
address (the laptop's extension becomes ``workocholic``), else the address itself. The tools stay
``browser.*`` — the loop, skills and reflection all key on that — and every one takes an optional
``browser`` argument naming where to act. Left out, a chat stays in the browser its work tab is
in, and a new job goes to the browser Arsen used last. The argument's text never names the
connected browsers: the tool list renders into the system prompt, and a list that changed with
every connect would re-prefill every conversation. Who is connected goes in the context block.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from jarvis_core.engine.current import current_conversation_id
from jarvis_proto import ToolImage, ToolResult, ToolResultKind, ToolSpec, new_id

log = logging.getLogger(__name__)

Sender = Callable[[dict[str, Any]], Awaitable[None]]

NOT_CONNECTED = "browser extension not connected"
TARGET_ARG = "browser"
TARGET_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Which browser to act in, by name (the context lists the connected ones). Leave it out to "
        "stay in the browser this chat already works in, or to use the one Arsen used last."
    ),
}

# An order, not a clock: two touches inside one tick of Windows' ~15 ms monotonic clock must
# still say which came last.
_touches = itertools.count()


@dataclass(eq=False)
class BrowserConn:
    """One connected extension."""

    name: str
    send: Sender
    agent: str
    version: str
    tools: list[ToolSpec]
    #: When Arsen last touched this browser (connect, or the side panel reporting a page).
    active_at: int = field(default_factory=lambda: next(_touches))
    context: dict[str, Any] | None = None
    #: Sessions that have browsed here and have not been told their job is over.
    working: set[str] = field(default_factory=set)


class WsProvider:
    name = "browser"

    def __init__(self, namer: Callable[[str | None], str | None] | None = None) -> None:
        #: Maps a peer address to a name (the Core looks it up among the MCP hosts).
        self.namer = namer
        self._conns: dict[str, BrowserConn] = {}
        #: call_id -> (connection name, future): a result finds its call whichever socket it is.
        self._pending: dict[str, tuple[str, asyncio.Future[ToolResult]]] = {}
        #: session -> the browser its work tab is in.
        self._affinity: dict[str, str] = {}
        # The union of the connected extensions' tools; kept after they go (see ws.py).
        self._tools: list[ToolSpec] = []
        # The last change to the tool set, for the context block: a conversation that learned
        # to sleep-and-re-read keeps doing it unless told a browser.wait now exists.
        self.changed_at: datetime | None = None
        self.added: list[str] = []
        self.removed: list[str] = []

    # --- state -----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self._conns)

    @property
    def error(self) -> str | None:
        return None if self._conns else NOT_CONNECTED

    @property
    def names(self) -> list[str]:
        return sorted(self._conns)

    def _latest(self) -> BrowserConn | None:
        return max(self._conns.values(), key=lambda c: c.active_at, default=None)

    @property
    def agent(self) -> str | None:
        latest = self._latest()
        return latest.agent if latest else None

    @property
    def version(self) -> str | None:
        latest = self._latest()
        return latest.version if latest else None

    @property
    def context(self) -> dict[str, Any] | None:
        """What Arsen is looking at, in the browser he used last."""
        latest = self._latest()
        return latest.context if latest else None

    # --- connection ----------------------------------------------------------------

    def resolve_name(self, hello: dict[str, Any], peer: str | None) -> str:
        explicit = str(hello.get("name") or "").strip()
        if explicit:
            return explicit
        if self.namer is not None:
            named = self.namer(peer)
            if named:
                return named
        return peer or "browser"

    def connect(self, send: Sender, hello: dict[str, Any], *, peer: str | None = None) -> BrowserConn:
        """Register one extension. The same name again replaces the old connection.

        A browser that reconnects before its old socket is noticed dead must not be locked out
        by its own ghost.
        """
        tools: list[ToolSpec] = []
        for raw in hello.get("tools") or []:
            try:
                spec = ToolSpec.model_validate(raw)
            except ValidationError as exc:
                log.warning("browser tool rejected: %s", exc)
                continue
            if not spec.name.startswith("browser."):
                spec = spec.model_copy(update={"name": f"browser.{spec.name.split('.', 1)[-1]}"})
            tools.append(spec.model_copy(update={"provider": "browser"}))
        name = self.resolve_name(hello, peer)
        old = self._conns.get(name)
        if old is not None:
            self._drop(old, "replaced by a new connection from the same browser")
        conn = BrowserConn(
            name=name,
            send=send,
            agent=str(hello.get("agent") or "browser"),
            version=str(hello.get("version") or ""),
            tools=tools,
        )
        self._conns[name] = conn
        self._retool()
        return conn

    def disconnect(self, conn: BrowserConn | None = None) -> None:
        """That connection is gone (all of them when none is named).

        Only the connection that is still registered: a socket replaced by a reconnect closes
        late and must not take its successor down with it.
        """
        conns = list(self._conns.values()) if conn is None else [conn]
        for c in conns:
            if self._conns.get(c.name) is c:
                self._drop(c, "browser extension disconnected")

    def _drop(self, conn: BrowserConn, why: str) -> None:
        del self._conns[conn.name]
        for call_id, (owner, fut) in list(self._pending.items()):
            if owner == conn.name:
                del self._pending[call_id]
                if not fut.done():
                    fut.set_result(ToolResult.failure(f"{why} ({conn.name})"))
        # The work tabs lived in that browser; a chat carrying on starts over wherever it lands.
        self._affinity = {s: n for s, n in self._affinity.items() if n != conn.name}

    def _retool(self) -> None:
        union: dict[str, ToolSpec] = {}
        for conn in sorted(self._conns.values(), key=lambda c: c.active_at):
            for spec in conn.tools:
                union[spec.name] = spec  # the most recent extension's version of a tool wins
        tools = [_with_target(s) for s in sorted(union.values(), key=lambda s: s.name)]
        before = {t.name for t in self._tools}
        after = {t.name for t in tools}
        if before and before != after:
            self.changed_at = datetime.now(UTC)
            self.added = sorted(after - before)
            self.removed = sorted(before - after)
        self._tools = tools

    async def job_done(self, session: str) -> None:
        """That session's run has ended: its work tab has nothing left to do.

        Only to a browser the session actually browsed in — a chat that never opened a page must
        not make an extension think about tabs at all. The extension decides what "go" means (it
        gives the tab a few minutes in case the next turn carries on with the same page).
        """
        for conn in list(self._conns.values()):
            if session not in conn.working:
                continue
            conn.working.discard(session)
            try:
                await conn.send({"type": "browser.job_done", "session": session})
            except Exception as exc:  # a closing socket is not worth failing a run for
                log.debug("browser job_done not delivered to %s: %s", conn.name, exc)

    def handle_result(self, frame: dict[str, Any]) -> None:
        entry = self._pending.pop(str(frame.get("call_id", "")), None)
        if entry is None or entry[1].done():
            return
        fut = entry[1]
        kind = str(frame.get("kind") or "data")
        text = str(frame.get("text") or "")
        images: list[ToolImage] = []
        img = frame.get("image")
        if isinstance(img, dict) and img.get("base64"):
            images.append(ToolImage(mime=str(img.get("mime") or "image/jpeg"), base64=str(img["base64"])))
        if kind == "error":
            fut.set_result(ToolResult.failure(str(frame.get("error") or text or "browser tool error")))
        elif kind == "empty" or not text.strip():
            fut.set_result(ToolResult.empty(text or "Nothing found."))
        else:
            fut.set_result(ToolResult(kind=ToolResultKind.DATA, text=text, images=images))

    def handle_context(self, frame: dict[str, Any], conn: BrowserConn | None = None) -> None:
        conn = conn or self._latest()
        if conn is None:
            return
        conn.context = {k: frame.get(k) for k in ("url", "title", "selection") if frame.get(k)}
        conn.active_at = next(_touches)  # the side panel is open there: Arsen is in that browser

    # --- ToolProvider --------------------------------------------------------------

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def _pick(self, target: str | None, session: str) -> BrowserConn | str:
        """The connection a call goes to, or why there is none."""
        if not self._conns:
            return NOT_CONNECTED
        if target:
            conn = self._conns.get(target) or next(
                (c for n, c in self._conns.items() if n.lower() == target.lower()), None
            )
            if conn is None:
                return f"no browser named {target!r} is connected; connected: {', '.join(self.names)}"
            return conn
        stuck = self._conns.get(self._affinity.get(session, ""))
        if stuck is not None:
            return stuck
        latest = self._latest()
        assert latest is not None
        return latest

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        timeout_s: float,
    ) -> ToolResult:
        # Which chat this is for: its own work tab, never another session's.
        session = current_conversation_id.get() or "default"
        arguments = dict(arguments)
        target = arguments.pop(TARGET_ARG, None)
        picked = self._pick(str(target) if target else None, session)
        if isinstance(picked, str):
            return ToolResult.failure(picked)
        conn = picked
        self._affinity[session] = conn.name
        conn.working.add(session)
        call_id = new_id("bcall")
        fut: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = (conn.name, fut)
        try:
            await conn.send(
                {
                    "type": "browser.call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "session": session,
                }
            )
        except Exception as exc:
            self._pending.pop(call_id, None)
            return ToolResult.failure(f"browser send failed ({conn.name}): {exc}")
        waiter = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait({fut, waiter}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()
            self._pending.pop(call_id, None)
            return ToolResult.failure(
                "cancelled" if waiter in done else f"browser tool timed out after {timeout_s:.0f}s ({conn.name})"
            )
        finally:
            waiter.cancel()

    def tools_changed_note(self, *, now: datetime | None = None, within: timedelta = timedelta(hours=24)) -> str | None:
        if self.changed_at is None or not (self.added or self.removed):
            return None
        if (now or datetime.now(UTC)) - self.changed_at > within:
            return None
        parts = []
        if self.added:
            parts.append("added " + ", ".join(self.added))
        if self.removed:
            parts.append("removed " + ", ".join(self.removed))
        return (
            "Browser tools changed recently: " + "; ".join(parts) + ". Read their descriptions — what a "
            "conversation learned to work around earlier may now have a tool of its own."
        )

    async def reload_extension(self) -> bool:
        """Ask every connected extension to reload itself (new files on disk after a deploy)."""
        sent = False
        for conn in list(self._conns.values()):
            try:
                await conn.send({"type": "browser.reload"})
                sent = True
            except Exception as exc:
                log.warning("browser.reload not sent to %s: %s", conn.name, exc)
        return sent

    def context_block(self) -> str | None:
        lines: list[str] = []
        latest = self._latest()
        if len(self._conns) > 1 and latest is not None:
            lines.append(
                f"Connected browsers: {', '.join(self.names)}. Arsen used {latest.name} last; "
                f"browser.* tools take `{TARGET_ARG}` to act in another."
            )
        context = latest.context if latest else None
        if context and context.get("url"):
            title = context.get("title") or ""
            where = f" (in {latest.name})" if latest and len(self._conns) > 1 else ""
            lines.append(f"Arsen is looking at{where}: {title} — {context['url']}")
            sel = context.get("selection")
            if sel:
                lines.append(f"Selected text: {str(sel)[:800]}")
        note = self.tools_changed_note()
        if note:
            lines.append(note)
        return "## Browser\n" + "\n".join(lines) if lines else None


def _with_target(spec: ToolSpec) -> ToolSpec:
    """The tool as the model sees it: its own arguments plus where to run it."""
    schema: dict[str, Any] = dict(spec.input_schema or {"type": "object"})
    raw = schema.get("properties")
    props: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
    if TARGET_ARG in props:
        return spec
    props[TARGET_ARG] = TARGET_SCHEMA
    schema["properties"] = props
    return spec.model_copy(update={"input_schema": schema})
