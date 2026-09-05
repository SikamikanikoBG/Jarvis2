"""The browser extension as a tool provider over the WebSocket (namespace ``browser``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from jarvis_proto import ToolResult, ToolResultKind, ToolSpec, new_id

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
        if kind == "error":
            fut.set_result(ToolResult.failure(str(frame.get("error") or text or "browser tool error")))
        elif kind == "empty" or not text.strip():
            fut.set_result(ToolResult.empty(text or "Nothing found."))
        else:
            fut.set_result(ToolResult(kind=ToolResultKind.DATA, text=text))

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

    def context_block(self) -> str | None:
        if not self.context or not self.context.get("url"):
            return None
        title = self.context.get("title") or ""
        sel = self.context.get("selection")
        block = f"## Browser\nArsen is looking at: {title} — {self.context['url']}"
        if sel:
            block += f"\nSelected text: {str(sel)[:800]}"
        return block
