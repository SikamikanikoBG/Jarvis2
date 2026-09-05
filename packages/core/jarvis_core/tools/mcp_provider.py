"""External MCP servers as tool providers.

One long-lived session per server, owned by a background task (the SDK's transports are
context managers that must be closed by the task that opened them). Tools appear as
``<server>.<tool>``; MCP annotations map onto engine policy: ``readOnlyHint`` → safe to
resume/retry, ``destructiveHint`` (only when the server says so explicitly) → confirmation.
A server that dies makes its tools return error results and reconnects on the next call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from jarvis_proto import McpServerSpec, McpTransport, ToolResult, ToolSpec

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15.0


class McpProvider:
    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.error: str | None = None
        self.tool_count = 0
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._session is not None

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._runner(), name=f"mcp-{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT_S)
        except TimeoutError:
            self.error = f"connect timeout after {CONNECT_TIMEOUT_S:.0f}s"
            log.warning("mcp %s: %s", self.name, self.error)
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        self._session = None

    async def _runner(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self.spec.transport is McpTransport.STDIO:
                    params = StdioServerParameters(
                        command=_resolve_command(self.spec.command or ""),
                        args=list(self.spec.args),
                        env={**os.environ, **self.spec.env},
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                else:
                    http = httpx.AsyncClient(
                        headers=self.spec.headers or None,
                        timeout=httpx.Timeout(float(self.spec.timeout_s), read=300.0),
                        follow_redirects=True,
                    )
                    await stack.enter_async_context(http)
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.spec.url or "", http_client=http)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._session = session
                self.error = None
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.error = _describe(exc)
            log.warning("mcp %s: %s", self.name, self.error)
        finally:
            self._session = None
            self._ready.set()

    async def _ensure(self) -> ClientSession:
        if self._session is None:
            await self.start()
        if self._session is None:
            raise RuntimeError(self.error or "not connected")
        return self._session

    # --- ToolProvider --------------------------------------------------------------

    async def list_tools(self) -> list[ToolSpec]:
        session = await self._ensure()
        result = await session.list_tools()
        specs: list[ToolSpec] = []
        for tool in result.tools:
            ann = tool.annotations
            read_only = bool(ann and ann.readOnlyHint)
            destructive = bool(ann and ann.destructiveHint is True and not read_only)
            idempotent = bool(ann and ann.idempotentHint) or read_only
            specs.append(
                ToolSpec(
                    name=f"{self.name}.{tool.name}",
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                    read_only=read_only,
                    destructive=destructive,
                    idempotent=idempotent,
                    provider=self.name,
                )
            )
        self.tool_count = len(specs)
        return specs

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        timeout_s: float,
    ) -> ToolResult:
        try:
            session = await self._ensure()
        except RuntimeError as exc:
            return ToolResult.failure(f"mcp server {self.name!r} unavailable: {exc}")
        inner = name.split(".", 1)[1] if "." in name else name
        call = asyncio.create_task(
            session.call_tool(inner, arguments, read_timeout_seconds=timedelta(seconds=timeout_s))
        )
        waiter = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if call not in done:
                call.cancel()
                return ToolResult.failure("cancelled")
            result = call.result()
        except Exception as exc:
            self.error = _describe(exc)
            await self.stop()
            return ToolResult.failure(f"mcp server {self.name!r}: {self.error}")
        finally:
            waiter.cancel()
        return _convert(result)


def _resolve_command(command: str) -> str:
    return sys.executable if command == "{python}" else command


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = ", ".join(_describe(e) for e in exc.exceptions[:3])
        return f"{inner}" if inner else type(exc).__name__
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _convert(result: Any) -> ToolResult:
    texts: list[str] = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", "") == "text":
            texts.append(item.text)
        elif getattr(item, "type", "") == "image":
            texts.append(f"[image {getattr(item, 'mimeType', '')}]")
        elif getattr(item, "type", "") == "resource":
            res = getattr(item, "resource", None)
            if res is not None and getattr(res, "text", None):
                texts.append(res.text)
    text = "\n".join(t for t in texts if t)
    if not text and getattr(result, "structuredContent", None):
        text = json.dumps(result.structuredContent, ensure_ascii=False)
    if getattr(result, "isError", False):
        return ToolResult.failure(text or "tool reported an error")
    if not text.strip():
        return ToolResult.empty()
    return ToolResult.data(text)
