"""The agent loop: model call → tool calls → repeat, with every rule from DESIGN §4.2 as code."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from jarvis_core.db import Store
from jarvis_core.engine.bus import EventBus
from jarvis_core.engine.context import ContextAssembler
from jarvis_core.engine.control import RunCancelledError, RunControl
from jarvis_core.engine.supervision import Emit, RunWatch, StepRecord, Supervisor, args_hash
from jarvis_core.features.knowledge import KnowledgeLearner
from jarvis_core.features.planner import PLAN_TOOLS, Planner
from jarvis_core.features.skills import SkillDetector
from jarvis_core.models.base import (
    ModelAdapter,
    ModelCancelled,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
)
from jarvis_core.tools import ToolRegistry
from jarvis_core.tools.facades import ExposurePolicy
from jarvis_proto import (
    Message,
    ModelUsage,
    Role,
    Run,
    RunKind,
    RunStatus,
    Settings,
    ThinkLevel,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from jarvis_proto.events import (
    ContextSkills,
    GuardArmed,
    GuardConsumed,
    MessageCreated,
    ModelCall,
    ModelDelta,
    ModelDone,
    PlanCreated,
    PlanStepDone,
    PlanStepStarted,
    RunDone,
    RunResumed,
    RunStarted,
    RunWaitingUser,
    ToolCallEvent,
    ToolConfirmRequested,
    ToolConfirmResolved,
    ToolResultEvent,
)
from jarvis_proto.runs import PlanStepStatus
from jarvis_proto.settings import RoleName

log = logging.getLogger(__name__)

_UNATTENDED = {RunKind.SCHEDULED, RunKind.TRIAGE, RunKind.MEETING, RunKind.SYSTEM}
_CONTEXT_KINDS = {RunKind.CHAT, RunKind.COLLAB, RunKind.SCHEDULED}
_TOOL_TIMEOUT_S = 120.0


class AdapterGetter(Protocol):
    def __call__(
        self, role: RoleName, *, think: bool | None = None, think_level: ThinkLevel | None = None
    ) -> ModelAdapter: ...


class AgentLoop:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        adapters: AdapterGetter,
        registry: ToolRegistry,
        context: ContextAssembler,
        supervisor: Supervisor,
        settings: Callable[[], Settings],
        *,
        policy: ExposurePolicy | None = None,
        planner: Planner | None = None,
        skills: SkillDetector | None = None,
        learner: KnowledgeLearner | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._adapters = adapters
        self._registry = registry
        self._context = context
        self._supervisor = supervisor
        self._settings = settings
        self._policy = policy or ExposurePolicy(mode="flat")
        self._planner = planner
        self._skills = skills
        self._learner = learner

    def _exposed_tools(self, plan_active: bool) -> list[ToolSpec]:
        s = self._settings()
        self._policy.mode = s.tool_exposure
        self._policy.threshold = s.facade_threshold
        tools = self._policy.expose(self._registry.specs())
        if plan_active:
            tools = [*tools, *PLAN_TOOLS]
        return tools

    # --- entry ---------------------------------------------------------------------

    async def run(self, run: Run, ctl: RunControl) -> None:
        emit = ctl.emitter.emit
        watch = RunWatch(budget=run.budget)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or datetime.now(UTC)
        run.waiting_reason = None
        await self._store.save_run(run)
        if ctl.resumed:
            await emit(RunResumed(run_id="", conversation_id="", from_seq=run.last_seq))
        else:
            await emit(RunStarted(run_id="", conversation_id=""))

        # Pre-flight: skills (structural triggers, else one cheap call) and the plan decision.
        skill_names: list[str] = []
        if self._skills is not None and run.kind in _CONTEXT_KINDS and not ctl.resumed:
            skill_names = await self._skills.detect(run.input_text)
            if skill_names:
                await emit(ContextSkills(run_id="", conversation_id="", names=skill_names))
        if (
            self._planner is not None
            and run.plan is None
            and not ctl.resumed
            and run.kind in _CONTEXT_KINDS
            and self._settings().planning_enabled
        ):
            pre = await self._planner.preflight(run.input_text)
            run.usage = run.usage.add(pre.usage)
            if pre.tier == "multi_step":
                namespaces = sorted({t.namespace for t in self._registry.specs()})
                plan, usage = await self._planner.make_plan(run.input_text, namespaces)
                run.usage = run.usage.add(usage)
                if plan is not None:
                    run.plan = plan
                    plan.steps[0].status = PlanStepStatus.IN_PROGRESS
                    await self._store.save_run(run)
                    await emit(PlanCreated(run_id="", conversation_id="", plan=plan))
                    await emit(PlanStepStarted(run_id="", conversation_id="", index=0, title=plan.steps[0].title))

        messages = await self._context.assemble(run, skill_names=skill_names, plan=run.plan)
        tools = self._exposed_tools(run.plan is not None)

        # Resume: an assistant message with tool calls that never got their results.
        run_messages = await self._store.list_run_messages(run.id)
        pending = _pending_tool_calls(run_messages)
        if pending is not None:
            assistant, calls = pending
            results = await self._execute_tool_calls(run, assistant, calls, ctl, watch, resumed=True)
            messages.extend(results)

        while True:
            self._check_cancel(ctl)
            if reason := self._supervisor.budget_exceeded(run, watch):
                await self._finish(run, ctl, messages, summary=reason)
                return

            # The system message carries plan progress and context; rebuild it each step.
            messages[0] = await self._context.system_message(run, skill_names=skill_names, plan=run.plan)
            run.steps_used += 1
            adapter = self._adapters(RoleName.CHAT, think=run.think, think_level=run.think_level)
            await emit(
                ModelCall(
                    run_id="",
                    conversation_id="",
                    role=RoleName.CHAT.value,
                    provider=adapter.spec.provider.value,
                    model=adapter.spec.model,
                    message_count=len(messages),
                    tool_count=len(tools),
                    think=adapter.spec.think,
                    think_level=adapter.spec.think_level,
                )
            )
            text, reasoning, raw_calls, usage, finish = await self._stream(adapter, messages, tools, run, ctl)
            calls = [self._policy.resolve(c) for c in raw_calls]  # facade op → canonical namespace.op
            run.usage = run.usage.add(usage)
            await emit(
                ModelDone(run_id="", conversation_id="", usage=usage, finish_reason=finish, tool_call_count=len(calls))
            )

            if not text.strip() and not calls:
                watch.empty_replies += 1
                if watch.empty_replies == 1:
                    await emit(
                        GuardArmed(run_id="", conversation_id="", guard="empty_reply", detail="no text, no tools")
                    )
                    nudge = await self._persist(
                        run,
                        Message.user("[supervisor] Your reply was empty. Answer the request now.", name="supervisor"),
                    )
                    messages.append(nudge)
                    await emit(GuardConsumed(run_id="", conversation_id="", guard="empty_reply", detail="nudged"))
                    continue
                raise RuntimeError("model returned an empty reply twice")

            assistant = await self._persist(run, Message.assistant(text, reasoning=reasoning or None, tool_calls=calls))
            messages.append(assistant)
            await self._store.save_run(run)

            if not calls:
                if run.plan is not None and run.plan.current_index is not None and not watch.plan_nudged:
                    # Final answer with open plan steps: one nudge (never a loop), then accept.
                    watch.plan_nudged = True
                    await emit(
                        GuardArmed(run_id="", conversation_id="", guard="open_plan", detail="answered with open steps")
                    )
                    open_steps = ", ".join(
                        s.title
                        for s in run.plan.steps
                        if s.status is PlanStepStatus.PENDING or s.status is PlanStepStatus.IN_PROGRESS
                    )
                    messages.append(
                        await self._persist(
                            run,
                            Message.user(
                                f"[supervisor] The plan still has open steps: {open_steps}. Finish them and mark each "
                                "with jarvis.plan_step_done, or skip them explicitly, then answer.",
                                name="supervisor",
                            ),
                        )
                    )
                    await emit(GuardConsumed(run_id="", conversation_id="", guard="open_plan", detail="nudged"))
                    continue
                await self._done(run, ctl, message_id=assistant.id)
                if self._learner is not None and run.kind in _CONTEXT_KINDS and self._settings().kg_learning:
                    self._learner.schedule(run.conversation_id, run.input_text, text, assistant.id)
                return

            results = await self._execute_tool_calls(run, assistant, calls, ctl, watch)
            messages.extend(results)

            verdict = await self._supervisor.review(run, watch, messages, emit, ctl.cancel)
            if verdict is None:
                continue
            if verdict.verdict == "stop":
                await self._finish(run, ctl, messages, summary=f"stopped by supervisor: {verdict.reason}")
                return
            if verdict.verdict == "nudge":
                messages.append(
                    await self._persist(
                        run,
                        Message.user(
                            f"[supervisor] {verdict.reason} Change approach or finish with what you have.",
                            name="supervisor",
                        ),
                    )
                )

    # --- model call ----------------------------------------------------------------

    async def _stream(
        self,
        adapter: ModelAdapter,
        messages: list[Message],
        tools: list[ToolSpec],
        run: Run,
        ctl: RunControl,
    ) -> tuple[str, str, list[ToolCall], ModelUsage, str | None]:
        emit = ctl.emitter.emit
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ToolCall] = []
        usage = ModelUsage(calls=1)
        finish: str | None = None
        try:
            async for chunk in adapter.stream(messages, tools, cancel=ctl.cancel):
                if isinstance(chunk, ModelTextChunk):
                    text_parts.append(chunk.text)
                    await emit(ModelDelta(run_id="", conversation_id="", kind="text", text=chunk.text))
                elif isinstance(chunk, ModelReasoningChunk):
                    reasoning_parts.append(chunk.text)
                    await emit(ModelDelta(run_id="", conversation_id="", kind="reasoning", text=chunk.text))
                elif isinstance(chunk, ModelToolCallsChunk):
                    calls.extend(chunk.calls)
                else:
                    usage = chunk.usage
                    finish = chunk.finish_reason
        except (ModelCancelled, asyncio.CancelledError):
            partial_id: str | None = None
            partial_text = "".join(text_parts)
            if partial_text.strip():
                msg = await self._persist(
                    run,
                    Message.assistant(partial_text, reasoning="".join(reasoning_parts) or None, partial=True),
                )
                partial_id = msg.id
            raise RunCancelledError(partial_id) from None
        return "".join(text_parts), "".join(reasoning_parts), calls, usage, finish

    # --- tools ---------------------------------------------------------------------

    async def _execute_tool_calls(
        self,
        run: Run,
        assistant: Message,
        calls: list[ToolCall],
        ctl: RunControl,
        watch: RunWatch,
        *,
        resumed: bool = False,
    ) -> list[Message]:
        emit = ctl.emitter.emit
        results: list[Message] = []
        for call in calls:
            self._check_cancel(ctl)
            if call.name in {"jarvis.plan_step_done", "jarvis.replan"}:
                results.append(await self._plan_tool(run, call, emit))
                continue
            spec = self._registry.get(call.name)
            read_only = spec.read_only if spec else False
            key = f"{run.id}:{call.id}"

            if resumed and not read_only:
                # We do not know whether the mutation happened. Ask, unless already answered.
                decision = await self._recorded_decision(run, call.id)
                if decision is None:
                    await self._wait_for_confirmation(
                        run,
                        ctl,
                        call,
                        reason="interrupted during this action — did it complete? Approve to run it again, reject to skip.",
                    )
                    decision = await self._recorded_decision(run, call.id)
                if decision is not None and not decision[0]:
                    results.append(
                        await self._tool_message(
                            run, call, ToolResult.failure("skipped by user after interruption"), 0, emit
                        )
                    )
                    continue
                key = f"{key}:retry"

            error = self._registry.validate(call.name, call.arguments)
            if error is not None:
                result = ToolResult.failure(error)
                await emit(
                    ToolCallEvent(
                        run_id="",
                        conversation_id="",
                        call_id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                        read_only=read_only,
                        idempotency_key=key,
                    )
                )
                results.append(await self._tool_message(run, call, result, 0, emit))
                watch.steps.append(
                    StepRecord(call.name, args_hash(call.name, call.arguments), result.kind, result.text)
                )
                continue

            if self._settings().confirmations.needs_confirmation(
                call.name, destructive=bool(spec and spec.destructive), unattended=run.kind in _UNATTENDED
            ):
                decision = await self._recorded_decision(run, call.id)
                if decision is None:
                    await self._wait_for_confirmation(run, ctl, call, reason="this action cannot be undone")
                    decision = await self._recorded_decision(run, call.id)
                if decision is not None and not decision[0]:
                    note = f" ({decision[1]})" if decision[1] else ""
                    results.append(
                        await self._tool_message(run, call, ToolResult.failure(f"rejected by user{note}"), 0, emit)
                    )
                    continue

            if not read_only and not await self._store.claim_idempotency(key, run.id, call.name):
                result = ToolResult.failure("duplicate call refused (idempotency key already used)")
                results.append(await self._tool_message(run, call, result, 0, emit))
                continue

            await emit(
                ToolCallEvent(
                    run_id="",
                    conversation_id="",
                    call_id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                    read_only=read_only,
                    idempotency_key=key,
                )
            )
            t0 = time.perf_counter()
            result = await self._registry.call(
                call.name, call.arguments, cancel=ctl.cancel, idempotency_key=key, timeout_s=_TOOL_TIMEOUT_S
            )
            duration = int((time.perf_counter() - t0) * 1000)
            if not read_only:
                await self._store.record_idempotent_result(key, result.model_dump_json())
            results.append(await self._tool_message(run, call, result, duration, emit))
            watch.steps.append(StepRecord(call.name, args_hash(call.name, call.arguments), result.kind, result.text))
        return results

    async def _tool_message(
        self, run: Run, call: ToolCall, result: ToolResult, duration_ms: int, emit: Emit
    ) -> Message:
        await emit(
            ToolResultEvent(
                run_id="", conversation_id="", call_id=call.id, name=call.name, result=result, duration_ms=duration_ms
            )
        )
        return await self._persist(run, Message.tool(call.id, call.name, result.to_model_text()))

    async def _wait_for_confirmation(self, run: Run, ctl: RunControl, call: ToolCall, *, reason: str) -> None:
        emit = ctl.emitter.emit
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[bool, str | None]] = loop.create_future()
        ctl.confirmations[call.id] = fut
        await emit(
            ToolConfirmRequested(
                run_id="", conversation_id="", call_id=call.id, name=call.name, arguments=call.arguments, reason=reason
            )
        )
        run.status = RunStatus.WAITING_USER
        run.waiting_reason = reason
        await self._store.save_run(run)
        await emit(RunWaitingUser(run_id="", conversation_id="", reason=reason, call_id=call.id))
        cancel_task = asyncio.create_task(ctl.cancel.wait())
        try:
            done, _ = await asyncio.wait({fut, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_task.cancel()
        if fut not in done:
            raise RunCancelledError(None)
        approved, note = fut.result()
        await emit(ToolConfirmResolved(run_id="", conversation_id="", call_id=call.id, approved=approved, note=note))
        run.status = RunStatus.RUNNING
        run.waiting_reason = None
        await self._store.save_run(run)

    async def _recorded_decision(self, run: Run, call_id: str) -> tuple[bool, str | None] | None:
        decision: tuple[bool, str | None] | None = None
        for ev in await self._store.list_events(run.id):
            if ev.get("type") == "tool.confirm_resolved" and ev.get("call_id") == call_id:
                decision = (bool(ev.get("approved")), ev.get("note"))
        return decision

    async def _plan_tool(self, run: Run, call: ToolCall, emit: Emit) -> Message:
        """Engine-owned tools: the model advances or replaces its own plan."""
        await emit(
            ToolCallEvent(
                run_id="",
                conversation_id="",
                call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                read_only=True,
                idempotency_key=f"{run.id}:{call.id}",
            )
        )
        plan = run.plan
        if call.name == "jarvis.replan":
            steps = [str(s).strip() for s in call.arguments.get("steps", []) if str(s).strip()]
            if not 2 <= len(steps) <= 8:
                result = ToolResult.failure("replan needs 2-8 steps")
            else:
                from jarvis_proto import Plan, PlanStep

                run.plan = Plan(
                    goal=str(call.arguments.get("goal", "")).strip() or (plan.goal if plan else ""),
                    steps=[PlanStep(title=s) for s in steps],
                )
                run.plan.steps[0].status = PlanStepStatus.IN_PROGRESS
                await self._store.save_run(run)
                await emit(PlanCreated(run_id="", conversation_id="", plan=run.plan))
                await emit(PlanStepStarted(run_id="", conversation_id="", index=0, title=steps[0]))
                result = ToolResult.data(f"Plan replaced with {len(steps)} steps; current step 1: {steps[0]}")
        elif plan is None:
            result = ToolResult.failure("there is no plan for this run")
        else:
            idx = int(call.arguments.get("index", 0)) - 1
            if not 0 <= idx < len(plan.steps):
                result = ToolResult.failure(f"step index must be 1..{len(plan.steps)}")
            else:
                plan.steps[idx].status = PlanStepStatus.DONE
                plan.steps[idx].note = str(call.arguments.get("note", "") or "")[:300] or None
                await emit(PlanStepDone(run_id="", conversation_id="", index=idx))
                nxt = plan.current_index
                if nxt is not None:
                    plan.steps[nxt].status = PlanStepStatus.IN_PROGRESS
                    await emit(PlanStepStarted(run_id="", conversation_id="", index=nxt, title=plan.steps[nxt].title))
                    result = ToolResult.data(f"Step {idx + 1} done. Current step {nxt + 1}: {plan.steps[nxt].title}")
                else:
                    result = ToolResult.data(f"Step {idx + 1} done. All steps complete — give the final answer.")
                await self._store.save_run(run)
        return await self._tool_message(run, call, result, 0, emit)

    # --- finishing -----------------------------------------------------------------

    async def _finish(self, run: Run, ctl: RunControl, messages: list[Message], *, summary: str) -> None:
        text = f"I stopped here: {summary}."
        last_assistant = next((m for m in reversed(messages) if m.role is Role.ASSISTANT and m.content.strip()), None)
        if last_assistant is not None:
            text += f"\n\nLast progress: {last_assistant.content.strip()[:400]}"
        msg = await self._persist(run, Message.assistant(text))
        await self._done(run, ctl, message_id=msg.id, summary=summary)

    async def _done(self, run: Run, ctl: RunControl, *, message_id: str | None, summary: str | None = None) -> None:
        run.status = RunStatus.DONE
        run.finished_at = datetime.now(UTC)
        await self._store.save_run(run)
        await ctl.emitter.emit(
            RunDone(
                run_id="",
                conversation_id="",
                message_id=message_id,
                usage=run.usage,
                steps_used=run.steps_used,
                summary=summary,
            )
        )

    # --- helpers -------------------------------------------------------------------

    async def _persist(self, run: Run, message: Message) -> Message:
        message.conversation_id = run.conversation_id
        message.run_id = run.id
        await self._store.add_message(message)
        # Conversation-level, so it bypasses the run emitter's seq and is not persisted twice.
        self._bus.publish(MessageCreated(message=message))
        return message

    @staticmethod
    def _check_cancel(ctl: RunControl) -> None:
        if ctl.cancel.is_set():
            raise RunCancelledError(None)


def _pending_tool_calls(run_messages: list[Message]) -> tuple[Message, list[ToolCall]] | None:
    if not run_messages:
        return None
    last_assistant = next((m for m in reversed(run_messages) if m.role is Role.ASSISTANT), None)
    if last_assistant is None or not last_assistant.tool_calls:
        return None
    answered = {m.tool_call_id for m in run_messages if m.role is Role.TOOL}
    pending = [c for c in last_assistant.tool_calls if c.id not in answered]
    return (last_assistant, pending) if pending else None
