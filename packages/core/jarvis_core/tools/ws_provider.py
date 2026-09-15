"""The browser extension as a tool provider over the WebSocket (namespace ``browser``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from jarvis_proto import ToolImage, ToolResult, ToolResultKind, ToolSpec, new_id

log = logging.getLogger(__name__)

Sender = Callable[[dict[str, Any]], Awaitable[None]]


class WsProvider:
    name = "browser"

    def __init__(self) -> None:
        self._send: Sender | None = None
        self._tools: list[ToolSpec] = []
        self._pending: dict[str, asyncio.Future[ToolResult]] = {}
        self.agent: str | None = None
        self.version: str | None = None
        self.context: dict[str, Any] | None = None
        self.error: str | None = "browser extension not connected"
        # The last change to the tool set, for the context block: a conversation that learned
        # to sleep-and-re-read keeps doing it unless told a browser.wait now exists.
        self.changed_at: datetime | None = None
        self.added: list[str] = []
        self.removed: list[str] = []

    @property
    def connected(self) -> bool:
        return self._send is not None

    # --- connection ----------------------------------------------------------------

    def connect(self, send: Sender, hello: dict[str, Any]) -> list[ToolSpec]:
        self._send = send
        self.agent = str(hello.get("agent") or "browser")
        self.version = str(hello.get("version") or "")
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
        before = {t.name for t in self._tools}
        after = {t.name for t in tools}
        if before and before != after:
            self.changed_at = datetime.now(UTC)
            self.added = sorted(after - before)
            self.removed = sorted(before - after)
        self._tools = tools
        self.error = None
        return tools

    def disconnect(self) -> None:
        self._send = None
        self._tools = []
        self.error = "browser extension not connected"
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(ToolResult.failure("browser extension disconnected"))
        self._pending.clear()

    def handle_result(self, frame: dict[str, Any]) -> None:
        fut = self._pending.pop(str(frame.get("call_id", "")), None)
        if fut is None or fut.done():
            return
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

    def handle_context(self, frame: dict[str, Any]) -> None:
        self.context = {k: frame.get(k) for k in ("url", "title", "selection") if frame.get(k)}

    # --- ToolProvider --------------------------------------------------------------

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        timeout_s: float,
    ) -> ToolResult:
        send = self._send
        if send is None:
            return ToolResult.failure("browser extension not connected")
        call_id = new_id("bcall")
        fut: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await send({"type": "browser.call", "call_id": call_id, "name": name, "arguments": arguments})
        except Exception as exc:
            self._pending.pop(call_id, None)
            return ToolResult.failure(f"browser send failed: {exc}")
        waiter = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait({fut, waiter}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()
            self._pending.pop(call_id, None)
            return ToolResult.failure(
                "cancelled" if waiter in done else f"browser tool timed out after {timeout_s:.0f}s"
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
        """Ask the connected extension to reload itself (new files on disk after a deploy)."""
        send = self._send
        if send is None:
            return False
        try:
            await send({"type": "browser.reload"})
        except Exception as exc:
            log.warning("browser.reload not sent: %s", exc)
            return False
        return True

    def context_block(self) -> str | None:
        lines: list[str] = []
        if self.context and self.context.get("url"):
            title = self.context.get("title") or ""
            lines.append(f"Arsen is looking at: {title} — {self.context['url']}")
            sel = self.context.get("selection")
            if sel:
                lines.append(f"Selected text: {str(sel)[:800]}")
        note = self.tools_changed_note()
        if note:
            lines.append(note)
        return "## Browser\n" + "\n".join(lines) if lines else None
