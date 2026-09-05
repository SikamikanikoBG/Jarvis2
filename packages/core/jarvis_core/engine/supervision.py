"""Supervision with power.

Budgets are mechanical and end the run with a summary. Structural signals (a repeated
identical call, an error streak) never act on their own: they *summon* the judge, and the
judge's verdict is executed by the loop. Every arming and every consumption is an event.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from jarvis_core.models.base import ModelAdapter, ModelDoneChunk, ModelTextChunk
from jarvis_proto import Message, Run, RunBudget, Settings, ToolResultKind
from jarvis_proto.events import GuardArmed, GuardConsumed, JudgeVerdict, RunEvent

log = logging.getLogger(__name__)

Emit = Callable[[RunEvent], Awaitable[None]]


@dataclass(slots=True)
class StepRecord:
    tool: str
    args_hash: str
    kind: ToolResultKind
    summary: str


@dataclass(slots=True)
class Verdict:
    verdict: Literal["continue", "nudge", "stop"]
    reason: str


@dataclass(slots=True)
class RunWatch:
    budget: RunBudget
    started: float = field(default_factory=time.monotonic)
    steps: list[StepRecord] = field(default_factory=list)
    empty_replies: int = 0
    nudges: int = 0

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started


def args_hash(tool: str, arguments: dict[str, object]) -> str:
    raw = json.dumps({"t": tool, "a": arguments}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class Supervisor:
    def __init__(
        self,
        settings: Callable[[], Settings],
        judge: Callable[[], ModelAdapter],
        *,
        error_streak: int = 3,
        max_nudges: int = 2,
    ) -> None:
        self._settings = settings
        self._judge = judge
        self._error_streak = error_streak
        self._max_nudges = max_nudges

    # --- budgets -------------------------------------------------------------------

    def budget_exceeded(self, run: Run, watch: RunWatch) -> str | None:
        b = watch.budget
        if run.steps_used >= b.max_steps:
            return f"step budget reached ({b.max_steps} model calls)"
        if run.usage.total_tokens >= b.max_tokens:
            return f"token budget reached ({run.usage.total_tokens:,} tokens)"
        if watch.elapsed_s() >= b.max_seconds:
            return f"time budget reached ({b.max_seconds}s)"
        return None

    # --- structural signals → judge -------------------------------------------------

    def signals(self, watch: RunWatch) -> list[str]:
        out: list[str] = []
        threshold = self._settings().repeated_call_threshold
        if watch.steps:
            last = watch.steps[-1]
            same = sum(1 for s in watch.steps if s.args_hash == last.args_hash)
            if same >= threshold:
                out.append(f"repeated identical call: {last.tool} x{same}")
            tail = watch.steps[-self._error_streak :]
            if len(tail) == self._error_streak and all(s.kind is ToolResultKind.ERROR for s in tail):
                out.append(f"{self._error_streak} consecutive tool errors")
        return out

    async def review(
        self, run: Run, watch: RunWatch, messages: list[Message], emit: Emit, cancel: asyncio.Event
    ) -> Verdict | None:
        signals = self.signals(watch)
        if not signals:
            return None
        detail = "; ".join(signals)
        await emit(GuardArmed(run_id="", conversation_id="", guard="progress", detail=detail))
        verdict = await self._ask_judge(run, watch, messages, detail, cancel)
        if verdict.verdict == "nudge" and watch.nudges >= self._max_nudges:
            verdict = Verdict("stop", f"{verdict.reason} (nudged {watch.nudges} times already)")
        await emit(JudgeVerdict(run_id="", conversation_id="", verdict=verdict.verdict, reason=verdict.reason))
        await emit(GuardConsumed(run_id="", conversation_id="", guard="progress", detail=verdict.verdict))
        if verdict.verdict == "nudge":
            watch.nudges += 1
        return verdict

    async def _ask_judge(
        self, run: Run, watch: RunWatch, messages: list[Message], detail: str, cancel: asyncio.Event
    ) -> Verdict:
        recent = "\n".join(f"- {s.tool}({s.args_hash}) -> {s.kind.value}: {s.summary[:120]}" for s in watch.steps[-8:])
        prompt = (
            "You supervise an AI assistant's task run. Decide whether it is making progress.\n"
            f"Task: {run.input_text[:600]}\n"
            f"Signal: {detail}\n"
            f"Recent tool steps (oldest first):\n{recent}\n\n"
            'Answer with JSON only: {"verdict": "continue" | "nudge" | "stop", "reason": "<one sentence>"}.\n'
            "continue = the repetition is justified (e.g. paging); nudge = it should change approach; "
            "stop = it is looping or cannot succeed."
        )
        try:
            adapter = self._judge()
            text = ""
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=cancel):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
                elif isinstance(chunk, ModelDoneChunk):
                    run.usage = run.usage.add(chunk.usage)
            return _parse_verdict(text)
        except Exception as exc:
            log.warning("judge unavailable: %s", exc)
            return Verdict("stop", f"supervisor could not evaluate progress ({type(exc).__name__}); stopping")


def _parse_verdict(text: str) -> Verdict:
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            v = str(data.get("verdict", "")).strip().lower()
            if v in {"continue", "nudge", "stop"}:
                return Verdict(v, str(data.get("reason", "")).strip() or "no reason given")  # type: ignore[arg-type]
        except json.JSONDecodeError:
            pass
    return Verdict("stop", f"judge answer was not parseable: {text[:120]!r}")
