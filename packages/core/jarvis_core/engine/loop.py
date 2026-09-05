"""The agent loop: model call → tool calls → repeat, with every rule from DESIGN §4.2 as code."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from jarvis_core.db import Store
from jarvis_core.engine.bus import EventBus
from jarvis_core.engine.context import ContextAssembler
from jarvis_core.engine.control import RunCancelledError, RunControl
from jarvis_core.engine.supervision import Emit, RunWatch, StepRecord, Supervisor, args_hash
from jarvis_core.models.base import (
    ModelAdapter,
    ModelCancelled,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
)
from jarvis_core.tools import ToolRegistry
from jarvis_proto import (
    Message,
    ModelUsage,
    Role,
    Run,
    RunKind,
    RunStatus,
    Settings,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from jarvis_proto.events import (
    GuardArmed,
    GuardConsumed,
    MessageCreated,
    ModelCall,
    ModelDelta,
    ModelDone,
    RunDone,
    RunResumed,
    RunStarted,
    RunWaitingUser,
    ToolCallEvent,
    ToolConfirmRequested,
    ToolConfirmResolved,
    ToolResultEvent,
)
from jarvis_proto.settings import RoleName

log = logging.getLogger(__name__)

_UNATTENDED = {RunKind.SCHEDULED, RunKind.TRIAGE, RunKind.MEETING, RunKind.SYSTEM}
_TOOL_TIMEOUT_S = 120.0


class AgentLoop:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        adapters: Callable[[RoleName], ModelAdapter],
        registry: ToolRegistry,
        context: ContextAssembler,
        supervisor: Supervisor,
        settings: Callable[[], Settings],
    ) -> None:
        self._store = store
        self._bus = bus
        self._adapters = adapters
        self._registry = registry
        self._context = context
        self._supervisor = supervisor
        self._settings = settings

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

        messages = await self._context.assemble(run)
        tools = self._registry.specs()

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

            run.steps_used += 1
            adapter = self._adapters(RoleName.CHAT)
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
                )
            )
            text, reasoning, calls, usage, finish = await self._stream(adapter, messages, tools, run, ctl)
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
                await self._done(run, ctl, message_id=assistant.id)
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

            if spec is not None and spec.destructive and run.kind not in _UNATTENDED:
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
