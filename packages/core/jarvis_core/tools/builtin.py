"""Core-owned tools, declared with a decorator and a pydantic argument model."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jarvis_proto import ToolResult, ToolResultKind, ToolSpec, new_id

if TYPE_CHECKING:
    from jarvis_core.tools.registry import ToolRegistry

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
# `wait_until` polls, so its cap is lower: it must return inside the core's own per-call ceiling
# (Settings.tool_timeout_max_s, 1200 s) with room to spare, and a wait this long that has not
# come true is news Arsen should hear rather than something to sit on.
WAIT_UNTIL_MAX_S = 900
IS_WAIT_TOOL = frozenset({"jarvis.wait", "jarvis.wait_until"})


class _WaitArgs(BaseModel):
    seconds: float = Field(description=f"How long to wait, 1-{WAIT_MAX_S}s. Chain calls for longer.", gt=0)
    reason: str = Field(
        default="",
        description="What is being waited for (a build, a reply, a person, a rate-limit window) — shown to Arsen.",
    )


class _WaitUntilArgs(BaseModel):
    tool: str = Field(description="The read-only tool to call again and again, e.g. 'workocholic.shell_run'.")
    args: dict[str, Any] = Field(
        default_factory=dict, description="Its arguments, exactly as you would pass them calling it yourself."
    )
    contains: str = Field(
        default="",
        description="Stop as soon as the result matches this. A regular expression; plain text works too.",
    )
    absent: str = Field(default="", description="Stop as soon as the result STOPS matching this.")
    timeout_s: float = Field(default=300, description=f"Give up after this long, 5-{WAIT_UNTIL_MAX_S}s.", gt=0)
    interval_s: float = Field(default=15, description="Seconds between attempts, 1-300.", gt=0)
    reason: str = Field(default="", description="What is being waited for — shown to Arsen.")


def _condition_met(result: ToolResult, has: re.Pattern[str] | None, lacks: re.Pattern[str] | None) -> bool:
    """A failed call is never the answer: an endpoint that refuses the connection is exactly the
    thing being waited out. With no pattern at all, the first call that does not fail wins."""
    if result.kind is ToolResultKind.ERROR:
        return False
    text = result.to_model_text()
    if has is not None and not has.search(text):
        return False
    return not (lacks is not None and lacks.search(text))


def _pattern(raw: str) -> re.Pattern[str] | None:
    if not raw:
        return None
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(raw), re.IGNORECASE)  # a bad regex is still good plain text


class CoreTools(BuiltinProvider):
    """Core-owned tools in the ``jarvis`` namespace — kept off the base class so feature
    providers that subclass BuiltinProvider do not each re-register them."""

    name = "builtin"

    def __init__(self, registry: Callable[[], ToolRegistry] | None = None) -> None:
        # A getter, not the registry: the registry is built after this provider and holds it.
        self._registry = registry
        super().__init__()

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
            "or use a schedule for hours or days. This is the blind pause: if there is anything you could "
            "CHECK — a health endpoint, a container's status, a file — jarvis.wait_until is the better tool, "
            "because it polls and comes back the moment it is true instead of after a guessed number of "
            "seconds. For a chatbot reply that will land on its own, browser.wait is better still — it "
            "returns the moment the page changes."
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

    @tool(
        "jarvis.wait_until",
        description=(
            "Call a read-only tool over and over until it says what you are waiting for, then carry on the "
            "SAME run with the answer. This is the retry loop you would otherwise write by hand at one model "
            "step per attempt: reach for it whenever something needs TIME to become true — a service or "
            "container coming up after a restart, a deploy or a build finishing, a file appearing, a queue "
            "draining, a long job leaving its lock behind. Pass the tool and its arguments exactly as you "
            "would call it yourself, plus `contains` (a regular expression the result must match) or "
            "`absent` (one it must stop matching); with neither, the first attempt that does not fail wins, "
            "which is the right test for an endpoint that is simply down. It returns the matching result "
            "itself, so there is no need to call the tool again afterwards. Two examples: a container — "
            "tool='workocholic.shell_run', args={'command': 'docker ps --filter name=app --format \"{{.Status}}\"'}, "
            "contains='Up '; an HTTP service — tool='fetch.fetch', args={'url': 'http://host:9800/healthz'}, "
            "contains='ok'. Only read-only tools can be polled, so nothing is sent or changed twice. The run "
            "clock is paused while it waits, so this never counts as taking too long or as a loop, and Arsen "
            "interrupting ends it at once. For a plain pause with nothing to check use jarvis.wait; for a page "
            "that will change on its own, browser.wait is better."
        ),
        args=_WaitUntilArgs,
        read_only=True,
    )
    async def _wait_until(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        contains: str = "",
        absent: str = "",
        timeout_s: float = 300,
        interval_s: float = 15,
        reason: str = "",
        *,
        cancel: asyncio.Event,
    ) -> ToolResult:
        registry = self._registry() if self._registry is not None else None
        if registry is None:
            return ToolResult.failure("jarvis.wait_until is not wired to the tool registry")
        name = tool.strip()
        if name in IS_WAIT_TOOL:
            return ToolResult.failure(f"{name} is itself a wait; use jarvis.wait for a plain pause")
        spec = registry.get(name)
        if spec is None:
            return ToolResult.failure(registry.cannot_route(name))
        if not spec.read_only:
            # Polling a mutation would send the mail, or press the button, once per attempt.
            return ToolResult.failure(
                f"jarvis.wait_until only repeats read-only tools, and {name} can change things. "
                "Wait on a read-only check of the same thing, then act once when it comes true."
            )
        if error := registry.validate(name, dict(args or {})):
            return ToolResult.failure(error)

        timeout = max(5.0, min(float(timeout_s), float(WAIT_UNTIL_MAX_S)))
        interval = max(1.0, min(float(interval_s), 300.0))
        has, lacks = _pattern(contains), _pattern(absent)
        tail = f" (waiting for {reason})" if reason else ""
        started = time.monotonic()
        deadline = started + timeout
        key = new_id("waituntil")
        attempts = 0
        last: ToolResult | None = None

        while True:
            attempts += 1
            remaining = deadline - time.monotonic()
            # Each attempt gets its own deadline: a call that hangs must not eat the whole wait.
            result = await registry.call(
                name,
                dict(args or {}),
                cancel=cancel,
                idempotency_key=f"{key}:{attempts}",
                timeout_s=max(5.0, min(120.0, remaining + 5.0)),
            )
            last = result
            waited = round(time.monotonic() - started)
            if _condition_met(result, has, lacks):
                what = (
                    f"matched {contains!r}"
                    if contains
                    else ("no longer matches " + repr(absent) if absent else "answered")
                )
                head = f"{name} {what} after {attempts} attempt{'s' if attempts != 1 else ''}, {waited}s{tail}:"
                return ToolResult.data(f"{head}\n{result.to_model_text()}")
            if cancel.is_set():
                break
            sleep_for = min(interval, deadline - time.monotonic())
            if sleep_for <= 0:
                break
            try:
                await asyncio.wait_for(cancel.wait(), timeout=sleep_for)
                break  # Arsen interrupted
            except TimeoutError:
                continue

        waited = round(time.monotonic() - started)
        seen = (last.to_model_text() if last is not None else "").strip()
        why = "cancelled" if cancel.is_set() else "still not true"
        return ToolResult.failure(
            f"{name} was {why} after {attempts} attempt{'s' if attempts != 1 else ''} over {waited}s{tail}. "
            f"Last result: {seen[:400] or '(empty)'}"
        )
