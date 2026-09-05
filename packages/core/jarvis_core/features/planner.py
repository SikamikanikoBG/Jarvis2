"""Pre-flight (tier + skills in one cheap call) and the planner. Both fail open."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from jarvis_core.models.base import ModelAdapter, ModelDoneChunk, ModelTextChunk
from jarvis_proto import Message, ModelUsage, Plan, PlanStep, ToolSpec

log = logging.getLogger(__name__)

_PREFLIGHT = """Classify this request for an AI assistant with tools.
Request: {message}

tier: "simple" if it can be answered or done in one or two tool calls; "multi_step" if it needs
several dependent steps (research then write, check several sources, multi-part deliverable).
Answer JSON only: {{"tier": "simple" | "multi_step"}}"""

_PLAN = """Write a short execution plan for this request.
The ONLY tools that exist: {tools}
Request: {message}

Rules: 2 to 6 concrete steps, each one action with a verifiable outcome; every step must be
doable with the tools listed above - do not plan a step no tool can perform, and never plan to
work around a missing tool with a shell command; no step for "reply". When the request changes
existing data (calendar, mail, files), the first step reads what is already there so nothing is
created twice.
Answer JSON only: {{"goal": "<one line>", "steps": ["<step 1>", "<step 2>", ...]}}"""


@dataclass(slots=True)
class Preflight:
    tier: str = "simple"
    usage: ModelUsage = field(default_factory=ModelUsage)


class Planner:
    def __init__(self, classifier: Any, planner: Any) -> None:
        self._classifier = classifier  # Callable[[], ModelAdapter]
        self._planner = planner

    async def preflight(self, message: str) -> Preflight:
        if len(message.strip()) < 25:
            return Preflight("simple")
        try:
            text, usage = await _ask(self._classifier(), _PREFLIGHT.format(message=message[:2000]))
            data = _json(text)
            tier = str(data.get("tier", "simple")).strip().lower() if data else "simple"
            return Preflight(tier if tier in {"simple", "multi_step"} else "simple", usage)
        except Exception as exc:
            log.warning("preflight skipped: %s", exc)
            return Preflight("simple")

    async def make_plan(self, message: str, tool_namespaces: list[str]) -> tuple[Plan | None, ModelUsage]:
        try:
            text, usage = await _ask(
                self._planner(), _PLAN.format(message=message[:3000], tools=", ".join(tool_namespaces) or "none")
            )
            data = _json(text)
            if not data:
                return None, usage
            steps = [str(s).strip() for s in data.get("steps", []) if str(s).strip()]
            if not 2 <= len(steps) <= 8:
                return None, usage
            goal = str(data.get("goal", "")).strip() or message[:120]
            plan = Plan(goal=goal, steps=[PlanStep(title=s) for s in steps])
            return plan, usage
        except Exception as exc:
            log.warning("planner skipped: %s", exc)
            return None, ModelUsage()


PLAN_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="jarvis.plan_step_done",
        description="Mark a plan step as done (1-based index) with a short note of the outcome.",
        input_schema={
            "type": "object",
            "properties": {"index": {"type": "integer", "minimum": 1}, "note": {"type": "string"}},
            "required": ["index"],
            "additionalProperties": False,
        },
        read_only=True,
        idempotent=True,
        provider="builtin",
    ),
    ToolSpec(
        name="jarvis.replan",
        description="Replace the plan when it no longer fits: new goal and 2-6 concrete steps.",
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 8},
            },
            "required": ["goal", "steps"],
            "additionalProperties": False,
        },
        read_only=True,
        idempotent=True,
        provider="builtin",
    ),
]


def plan_block(plan: Plan) -> str:
    lines = [f"## Plan (goal: {plan.goal})"]
    current = plan.current_index
    for i, step in enumerate(plan.steps):
        mark = "x" if step.status.value == "done" else ("-" if step.status.value == "skipped" else " ")
        cur = "  ← current" if i == current else ""
        lines.append(f"{i + 1}. [{mark}] {step.title}{cur}")
    lines.append(
        "Work the current step, then call jarvis.plan_step_done with its index (1-based). "
        "Call jarvis.replan if the plan no longer fits. Give the final answer only when every step is done or skipped."
    )
    return "\n".join(lines)


async def _ask(adapter: ModelAdapter, prompt: str) -> tuple[str, ModelUsage]:
    text = ""
    usage = ModelUsage()
    async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
        if isinstance(chunk, ModelTextChunk):
            text += chunk.text
        elif isinstance(chunk, ModelDoneChunk):
            usage = chunk.usage
    return text, usage


def _json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
