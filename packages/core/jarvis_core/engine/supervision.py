"""Supervision with power.

Budgets are mechanical and end the run with a summary. Structural signals (a repeated
identical call, an error streak) never act on their own: they *summon* the judge, and the
judge's verdict is executed by the loop. Every arming and every consumption is an event.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from jarvis_core.models.base import ModelAdapter, ModelDoneChunk, ModelTextChunk
from jarvis_proto import Message, Run, RunBudget, Settings, ToolResult, ToolResultKind
from jarvis_proto.events import GuardArmed, GuardConsumed, JudgeVerdict, RunEvent

log = logging.getLogger(__name__)

Emit = Callable[[RunEvent], Awaitable[None]]


@dataclass(slots=True)
class StepRecord:
    tool: str
    args_hash: str
    #: Fingerprint of what the call came back with. The other half of a step's identity — see
    #: `Supervisor.signals`.
    result_hash: str
    #: The arguments as written, for the judge to read. A hash tells it nothing.
    args_summary: str
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
    plan_nudged: bool = False
    # The output allowance ran out before the model wrote anything, so thinking was switched off
    # for one step to give the answer the whole budget. Once per run.
    thinking_off_retry: bool = False
    # An answer was cut off at the allowance and the model was asked to carry on. Once per run.
    continued: bool = False

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started


def args_hash(tool: str, arguments: dict[str, object]) -> str:
    raw = json.dumps({"t": tool, "a": arguments}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def result_hash(text: str) -> str:
    """What a call came back with, as a short digest.

    Arguments alone are not the identity of a call. A tool that acts on state its arguments do
    not name — a browser tab, a cursor, a clock — answers `{"mode": "text"}` differently every
    time, and then the only thing that says whether the work moved is the answer.
    """
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def step_of(tool: str, arguments: dict[str, object], result: ToolResult) -> StepRecord:
    """One record from a finished call — the single place a step's identity is assembled."""
    return StepRecord(
        tool=tool,
        args_hash=args_hash(tool, arguments),
        result_hash=result_hash(result.text),
        args_summary=json.dumps(arguments, ensure_ascii=False, default=str)[:300],
        kind=result.kind,
        summary=result.text,
    )


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
        # Against tokens the model actually READ, not against the conversation counted once per
        # step. Measured on ardi 2026-09-06: a chat stopped at "224,594 tokens" of which 203,840
        # had been served from the prefix cache — 20k of work judged against a 200k budget. A
        # runaway loop still trips it, because a loop keeps adding new tokens.
        if run.usage.processed_tokens >= b.max_tokens:
            detail = f"{run.usage.processed_tokens:,} tokens"
            if run.usage.cached_tokens:
                detail += f" read and written, on top of {run.usage.cached_tokens:,} served from the prompt cache"
            return f"token budget reached ({detail})"
        if watch.elapsed_s() >= b.max_seconds:
            return f"time budget reached ({b.max_seconds}s)"
        return None

    # --- structural signals → judge -------------------------------------------------

    def signals(self, watch: RunWatch) -> list[str]:
        """Structural evidence that the run is going nowhere. Never acts — it summons the judge.

        A repeat is the same arguments AND the same answer. Matching on the arguments alone
        called a loop on work that was moving: `browser.read {"mode": "text"}` carries nothing
        that varies, because the page it reads was chosen by the `browser.open` before it, so a
        correct open/read/open/read walk across five different pages looked like one call made
        five times. On 2026-09-09 that stopped the DevBG scan twice, two minutes in. Same call
        with the same answer is still a loop, and still trips this.
        """
        out: list[str] = []
        threshold = self._settings().repeated_call_threshold
        if watch.steps:
            last = watch.steps[-1]
            same = sum(1 for s in watch.steps if s.args_hash == last.args_hash and s.result_hash == last.result_hash)
            if same >= threshold:
                out.append(f"repeated identical call with an identical result: {last.tool} x{same}")
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
        prompt = (
            "You supervise an AI assistant's task run. Decide whether it is making progress.\n"
            f"Task: {run.input_text[:600]}\n"
            f"Signal: {detail}\n"
            f"Recent tool steps (oldest first):\n{render_steps(watch.steps[-8:])}\n\n"
            "Some tools act on state their arguments do not name (a browser tab, a cursor). For\n"
            "those, identical arguments are normal and say nothing; judge by whether the RESULTS\n"
            "move forward.\n"
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


def _shared_head(values: Sequence[str], minimum: int = 40) -> str:
    """The prefix every value starts with, when it is long enough to be worth stating once."""
    if len(values) < 2:
        return ""
    head = values[0]
    for value in values[1:]:
        cut = 0
        limit = min(len(head), len(value))
        while cut < limit and head[cut] == value[cut]:
            cut += 1
        head = head[:cut]
        if len(head) < minimum:
            return ""
    return head if len(head) >= minimum else ""


def render_steps(steps: Sequence[StepRecord], width: int = 200) -> str:
    """The recent tool steps, written so the judge can tell them APART.

    This used to be `summary[:120]`, and for a tool whose answer opens with a long fixed
    preamble that was the same 120 characters every time: on 2026-09-09 the judge was handed
    five lines whose first ~100 characters were all `[tab 1271060609] (20+) DevBG | Facebook —
    https://www.facebook.com/groups/1401603840076709/search/?q=`, with nothing but
    percent-encoded Cyrillic past the cut, and it read them as one call repeating. So: the
    shared head is stated once, each line then carries what is actually its own,
    percent-encoding is decoded (`%D1%81%D1%8A` cannot be reasoned about), and the arguments
    are shown instead of a hash of them — the difference between two steps is often exactly
    there.
    """
    if not steps:
        return "(none)"
    summaries = [_readable(s.summary) for s in steps]
    head = _shared_head(summaries)
    lines: list[str] = []
    if head:
        lines.append(f"(every result below begins {_clip(head, 160)!r} — only what follows differs)")
    for step, summary in zip(steps, summaries, strict=True):
        rest = summary[len(head) :] if head else summary
        lines.append(
            f"- {step.tool} {_clip(_readable(step.args_summary), 100)} -> {step.kind.value}"
            f" ({len(step.summary)} chars): {_clip(rest, width) or '(nothing past the shared head)'}"
        )
    return "\n".join(lines)


def _readable(text: str) -> str:
    """Percent-decoded and on one line; a URL-encoded query is unreadable to any reader."""
    # A malformed escape must not cost us the review, so a failed decode leaves the text as is.
    with contextlib.suppress(Exception):
        text = urllib.parse.unquote(text)
    return " ".join(text.split())


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "\u2026"


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
