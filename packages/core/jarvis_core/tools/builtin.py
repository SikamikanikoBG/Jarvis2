"""Core-owned tools, declared with a decorator and a pydantic argument model."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jarvis_proto import ToolResult, ToolSpec

log = logging.getLogger(__name__)

ToolFn = Callable[..., Awaitable[ToolResult]]


@dataclass(slots=True)
class _Entry:
    spec: ToolSpec
    fn: ToolFn
    args_model: type[BaseModel] | None


def tool(
    name: str,
    *,
    description: str,
    args: type[BaseModel] | None = None,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
) -> Callable[[ToolFn], ToolFn]:
    def deco(fn: ToolFn) -> ToolFn:
        schema = args.model_json_schema() if args else {"type": "object", "properties": {}}
        schema.pop("title", None)
        fn.__jarvis_tool__ = _Entry(  # type: ignore[attr-defined]
            spec=ToolSpec(
                name=name,
                description=description,
                input_schema=schema,
                read_only=read_only,
                destructive=destructive,
                idempotent=idempotent,
                provider="builtin",
            ),
            fn=fn,
            args_model=args,
        )
        return fn

    return deco


class BuiltinProvider:
    name = "builtin"

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        for _, member in inspect.getmembers(self, predicate=callable):
            entry = getattr(member, "__jarvis_tool__", None)
            if isinstance(entry, _Entry):
                self._entries[entry.spec.name] = _Entry(entry.spec, cast(ToolFn, member), entry.args_model)

    def register(self, fn: ToolFn) -> None:
        entry = getattr(fn, "__jarvis_tool__", None)
        if not isinstance(entry, _Entry):
            raise TypeError("register() expects a @tool-decorated coroutine")
        self._entries[entry.spec.name] = entry

    async def list_tools(self) -> list[ToolSpec]:
        return [e.spec for e in self._entries.values()]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        timeout_s: float,
    ) -> ToolResult:
        entry = self._entries[name]
        kwargs: dict[str, Any] = {}
        if entry.args_model is not None:
            kwargs = entry.args_model.model_validate(arguments).model_dump()
        sig = inspect.signature(entry.fn)
        if "cancel" in sig.parameters:
            kwargs["cancel"] = cancel
        return await entry.fn(**kwargs)


class _TimeArgs(BaseModel):
    timezone: str = Field(default="Europe/Sofia", description="IANA timezone name")


# A deliberate wait is not a runaway tool: the run clock is paused while it sleeps (the loop
# credits it back), so a long wait does not eat the time budget. The cap is per call — chain
# calls for longer, or a schedule for hours/days (a fired schedule is a fresh run).
WAIT_MAX_S = 3600
IS_WAIT_TOOL = frozenset({"jarvis.wait"})


class _WaitArgs(BaseModel):
    seconds: float = Field(description=f"How long to wait, 1-{WAIT_MAX_S}s. Chain calls for longer.", gt=0)
    reason: str = Field(
        default="",
        description="What is being waited for (a build, a reply, a person, a rate-limit window) — shown to Arsen.",
    )


class CoreTools(BuiltinProvider):
    """Core-owned tools in the ``jarvis`` namespace — kept off the base class so feature
    providers that subclass BuiltinProvider do not each re-register them."""

    name = "builtin"

    @tool(
        "jarvis.time",
        description="Current date and time. Use it before anything that depends on today's date.",
        args=_TimeArgs,
        read_only=True,
        idempotent=True,
    )
    async def _time(self, timezone: str = "Europe/Sofia") -> ToolResult:
        try:
            now = datetime.now(ZoneInfo(timezone))
        except Exception:
            return ToolResult.failure(f"unknown timezone {timezone!r}")
        return ToolResult.data(now.strftime("%A, %Y-%m-%d %H:%M:%S %Z"))

    @tool(
        "jarvis.wait",
        description=(
            "Pause, then carry on the SAME run. Use it to keep a task alive across something that takes real "
            "time — a build or deploy finishing, a rate-limit window, a page or a person you have asked and are "
            "waiting on — instead of giving up and asking Arsen to prompt you again. The run clock is paused "
            "while you wait, so this does not count as taking too long or as a loop. Cancellable: if Arsen "
            f"interrupts, the wait ends at once. One call waits up to {WAIT_MAX_S}s; call it again for longer, "
            "or use a schedule for hours or days. For a chatbot reply that will land on its own, browser.wait "
            "is better — it returns the moment the page changes."
        ),
        args=_WaitArgs,
        read_only=True,
    )
    async def _wait(self, seconds: float, reason: str = "", *, cancel: asyncio.Event) -> ToolResult:
        want = max(1.0, min(float(seconds), float(WAIT_MAX_S)))
        started = time.monotonic()
        try:
            await asyncio.wait_for(cancel.wait(), timeout=want)
            interrupted = True
        except TimeoutError:
            interrupted = False
        waited = round(time.monotonic() - started)
        tail = f" (waiting for {reason})" if reason else ""
        if interrupted:
            return ToolResult.data(f"Wait interrupted after {waited}s{tail} — continue with the task now.")
        return ToolResult.data(f"Waited {waited}s{tail}. Carry on.")
