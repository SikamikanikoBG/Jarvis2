"""The agent loop: model call → tool calls → repeat, with every rule from DESIGN §4.2 as code."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from jarvis_core.db import Store
from jarvis_core.engine.bus import EventBus
from jarvis_core.engine.context import (
    ContextAssembler,
    admit,
    context_breakdown,
    last_turn_skills,
    prompt_chars,
)
from jarvis_core.engine.control import RunCancelledError, RunControl
from jarvis_core.engine.supervision import Emit, RunWatch, StepRecord, Supervisor, args_hash, step_of
from jarvis_core.engine.views import AGED_VIEW_CHARS, aged, view_of
from jarvis_core.features.outbound import unasked_outbound
from jarvis_core.features.planner import PLAN_TOOLS
from jarvis_core.features.shadow import ShadowRecorder, preflight_questions
from jarvis_core.models.base import (
    ModelAdapter,
    ModelCancelled,
    ModelError,
    ModelReasoningChunk,
    ModelTextChunk,
    ModelToolCallsChunk,
)
from jarvis_core.tools import ToolRegistry
from jarvis_core.tools.builtin import IS_WAIT_TOOL
from jarvis_core.tools.facades import ExposurePolicy
from jarvis_proto import (
    Channel,
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
    ToolResultKind,
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
    RunSteered,
    RunWaitingUser,
    ToolCallEvent,
    ToolConfirmRequested,
    ToolConfirmResolved,
    ToolResultEvent,
)
from jarvis_proto.runs import PlanStepStatus
from jarvis_proto.settings import RoleName

if TYPE_CHECKING:
    # Type-only: these live in jarvis_core.features, which imports jarvis_core.engine.bus, which
    # runs jarvis_core.engine.__init__, which imports this module. Importing them for real made
    # `import jarvis_core.features.knowledge` (or .skills) as the FIRST jarvis_core import raise a
    # circular ImportError — invisible from the app, which always reaches them via app.py.
    from jarvis_core.features.attachments import AttachmentStore
    from jarvis_core.features.knowledge import KnowledgeLearner
    from jarvis_core.features.planner import Planner, Preflight
    from jarvis_core.features.reflection import PlaybookReflector
    from jarvis_core.features.skills import SkillDetector

log = logging.getLogger(__name__)

# A task (RunKind.TASK) is deliberately NOT here: nobody watches it, and that is exactly why every
# action of it that is not read-only waits for Arsen (settings.tasks, below) - the opposite of what
# "unattended" means for a schedule, whose prompt is what authorised it.
_UNATTENDED = {RunKind.SCHEDULED, RunKind.TRIAGE, RunKind.MEETING, RunKind.SYSTEM}
_CONTEXT_KINDS = {RunKind.CHAT, RunKind.COLLAB, RunKind.SCHEDULED, RunKind.TASK}
# How often a task held between steps looks whether the call is over.
_CALL_HOLD_POLL_S = 1.0
# A message this short after a turn that ran on skills is taken as a follow-up to it.
_FOLLOW_UP_CHARS = 160

# The tools whose whole purpose is to remember: the notes boards and the knowledge graph. An
# incognito chat offers neither (reading them is fine - it is what goes OUT that must stop).
# Everything else stays, including schedules and mail: those are things Arsen asks for by
# name, and a private chat is not a crippled one.
_MEMORY_NAMESPACES = {"notes", "kg"}


def is_memory_writer(spec: ToolSpec) -> bool:
    return spec.namespace in _MEMORY_NAMESPACES and not spec.read_only


_TOOL_TIMEOUT_S = 120.0


class AdapterGetter(Protocol):
    def __call__(
        self,
        role: RoleName,
        *,
        think: bool | None = None,
        think_level: ThinkLevel | None = None,
        kind: RunKind | None = None,
        exact: bool = False,
    ) -> ModelAdapter: ...


class TaskGate(Protocol):
    """What the loop asks delegated tasks (features/tasks.py) - one place, so the loop knows the
    rules exist without knowing them."""

    def hidden(self, run: Run, spec: ToolSpec) -> bool:
        """Not offered to this run at all."""
        ...

    async def refusal(self, run: Run, name: str, *, steered_by_other: bool) -> str | None:
        """Why this run may not make this call, or None. A model can name a tool it was not
        offered, so what ``hidden`` hides is refused here again."""
        ...

    async def unreachable(self, run: Run, namespace: str, reason: str) -> bool:
        """A task's tool came back from a machine that is down. True: the task ends on it (the
        brief needs that machine); False: an ordinary failed call it works around."""
        ...

    def should_hold(self, run: Run) -> bool:
        """This task waits before its next step: a call is open, or enough other tasks are working."""
        ...

    async def paused(self, run: Run, on: bool) -> None: ...


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
        attachments: AttachmentStore | None = None,
        reflector: PlaybookReflector | None = None,
        windows: Callable[[RunKind], Awaitable[int | None]] | None = None,
        shadow: ShadowRecorder | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._adapters = adapters
        # The context window of a run kind's lane, in tokens (AdapterFactory.context_window);
        # None when nobody knows, and the budgets stay as configured.
        self._windows = windows
        self._admit_chars = 48_000  # per step, from the view (Settings.admit_chars)
        self._registry = registry
        self._context = context
        self._supervisor = supervisor
        self._settings = settings
        self._policy = policy or ExposurePolicy(mode="flat")
        self._planner = planner
        self._skills = skills
        self._learner = learner
        self._attachments = attachments
        self._reflector = reflector
        # Questions asked aloud on a call before an action that needs his go-ahead: when (see
        # _voice_confirmation), by conversation and the exact call.
        self._voice_asks: dict[tuple[str, str, str], tuple[datetime, str | None]] = {}
        self._shadow = shadow
        self._tasks: TaskGate | None = None

    def set_task_gate(self, gate: TaskGate | None) -> None:
        """Delegated tasks' rules (features/tasks.py), wired after both exist."""
        self._tasks = gate

    def _shadow_preflight(
        self, run: Run, skill_names: list[str], trace: dict[str, object], pre: Preflight | None, pre_ms: float
    ) -> None:
        """The same request to Laya, next to the 27B's tier and skills (features/shadow.py). The
        skill list is read in the background task, never on the run's path."""
        shadow, skills = self._shadow, self._skills
        if shadow is None or not self._settings().shadow.observes("preflight"):
            return

        async def go() -> None:
            listed = [(s.name, s.description or "") for s in await skills.store.list() if s.enabled] if skills else []
            shadow.observe(
                "preflight",
                ref=run.id,
                source=run.kind.value,
                input={"request": run.input_text[:2000]},
                questions=preflight_questions(listed),
                prod={
                    "tier": pre.tier if pre is not None else None,
                    "tier_by": "model" if pre is not None and len(run.input_text.strip()) >= 25 else "rule",
                    "skills": skill_names,
                    "skills_by": trace.get("by", "none"),
                },
                prod_ms=pre_ms,
            )

        task = asyncio.create_task(go(), name="shadow:preflight")
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _exposed_tools(
        self, plan_active: bool, *, incognito: bool = False, voice: bool = False, run: Run | None = None
    ) -> list[ToolSpec]:
        s = self._settings()
        self._policy.mode = s.tool_exposure
        self._policy.threshold = s.facade_threshold
        specs = [t for t in self._registry.specs() if not t.internal]
        if incognito:
            specs = [t for t in specs if not is_memory_writer(t)]
        if voice:
            # On a call he thinks and remembers; he does not act (settings.voice.namespaces).
            specs = [t for t in specs if s.voice.allows(t.namespace)]
        if run is not None and self._tasks is not None:
            gate = self._tasks
            specs = [t for t in specs if not gate.hidden(run, t)]
        tools = self._policy.expose(specs)
        if plan_active:
            tools = [*tools, *PLAN_TOOLS]
        return tools

    async def _is_incognito(self, run: Run) -> bool:
        conv = await self._store.get_conversation(run.conversation_id)
        return bool(conv is not None and conv.incognito)

    # --- entry ---------------------------------------------------------------------

    async def run(self, run: Run, ctl: RunControl) -> None:
        emit = ctl.emitter.emit
        watch = RunWatch(budget=run.budget)
        # The run executes on one lane (settings.run_routing): the main loop as the lane's spec,
        # every other model call it makes on the lane's endpoint — so a scheduled run chewing
        # through 84k-token steps cannot starve the chat Arsen is typing in.
        main_role = self._settings().lane_for(run.kind)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or datetime.now(UTC)
        run.waiting_reason = None
        await self._store.save_run(run)
        if ctl.resumed:
            await emit(RunResumed(run_id="", conversation_id="", from_seq=run.last_seq))
        else:
            await emit(RunStarted(run_id="", conversation_id=""))

        # Pre-flight: skills (structural triggers, else one cheap call) and the plan decision.
        # Nothing is streamed while these run, so they are pure waiting for Arsen — and they ask
        # the same model two unrelated questions about the same sentence. They are independent,
        # so they go together; whether that actually overlaps is up to the endpoint's concurrency
        # limit, which is why `max_concurrent_runs_per_endpoint` below 2 makes this a no-op
        # rather than a bug.
        wants_skills = self._skills is not None and run.kind in _CONTEXT_KINDS and not ctl.resumed
        voice = run.channel is Channel.VOICE
        wants_tier = (
            self._planner is not None
            and run.plan is None
            and not ctl.resumed
            and run.kind in _CONTEXT_KINDS
            and self._settings().planning_enabled
            and not voice  # a call is a conversation, never a multi-step plan
        )
        skill_names: list[str] = []
        pre: Preflight | None = None
        skill_trace: dict[str, object] = {}  # how the detector decided, for the shadow record
        # Handing the work to a background task is one of the shapes a turn can take, decided
        # with the others BEFORE a plan exists - a plan written first was a plan to do the work
        # here and then to wait for the task (alpha 2026-10-30, F-1/F-2).
        delegate_spec = self._registry.get("tasks.delegate")
        can_delegate = (
            self._tasks is not None and delegate_spec is not None and not self._tasks.hidden(run, delegate_spec)
        )
        t_pre = time.perf_counter()
        if wants_skills and wants_tier:
            assert self._skills is not None and self._planner is not None
            skill_names, pre = await asyncio.gather(
                self._skills.detect(run.input_text, trace=skill_trace),
                self._planner.preflight(run.input_text, can_delegate=can_delegate),
            )
        elif wants_skills:
            assert self._skills is not None
            # On a call the detector may look, but not ask: a model round trip here is a second
            # of silence on the line before the answer has even begun (2026-09-20).
            skill_names = await self._skills.detect(run.input_text, allow_model=not voice, trace=skill_trace)
        elif wants_tier:
            assert self._planner is not None
            pre = await self._planner.preflight(run.input_text, can_delegate=can_delegate)
        hand_off = can_delegate and pre is not None and pre.tier == "delegate"
        pre_ms = (time.perf_counter() - t_pre) * 1000
        if wants_skills:
            skill_names = await self._follow_up_skills(run, skill_names, skill_trace)
        if skill_names:
            await emit(ContextSkills(run_id="", conversation_id="", names=skill_names))
        if pre is not None and self._planner is not None:
            run.usage = run.usage.add(pre.usage)
            if pre.tier == "multi_step":
                # Tool NAMES, not namespaces: given only "workocholic" the planner invented a
                # "recolour calendar categories" step no tool could do, and the model then reached
                # for shell_run to poke Outlook COM by hand (2026-09-05 Burnout run).
                tool_names = sorted(t.name for t in self._registry.specs())
                plan, usage = await self._planner.make_plan(run.input_text, tool_names)
                run.usage = run.usage.add(usage)
                if plan is not None:
                    run.plan = plan
                    plan.steps[0].status = PlanStepStatus.IN_PROGRESS
                    await self._store.save_run(run)
                    await emit(PlanCreated(run_id="", conversation_id="", plan=plan))
                    await emit(PlanStepStarted(run_id="", conversation_id="", index=0, title=plan.steps[0].title))

        incognito = await self._is_incognito(run)
        if (wants_skills or wants_tier) and not voice and not incognito:
            self._shadow_preflight(run, skill_names, skill_trace, pre, pre_ms)
        tools = self._exposed_tools(run.plan is not None, incognito=incognito, voice=voice, run=run)
        # Built against the lane's window: the tool schemas and the system message are the fixed
        # part of every step's prompt, and what is left after them, the answer and the reserve is
        # what history and this run's results may share.
        window = await self._windows(run.kind) if self._windows is not None else None
        lane_spec = self._adapters(main_role).spec
        view = await self._context.assemble_view(
            run, skill_names=skill_names, tools=tools, window=window, max_tokens=lane_spec.max_tokens
        )
        messages = view.messages
        self._admit_chars = view.admit_chars
        plan_trailer: Message | None = None  # ephemeral, always the last message
        think_off_once = False  # set for ONE step when reasoning ate the whole output allowance
        # Adaptive thinking: the first step of a run always reasons, and after that only a step
        # with something new to work out does. Set by the tail of the previous step, consumed at
        # the top of the next one.
        # Handing off is a tool call, not a problem to reason through: the first step of a turn
        # the pre-flight sent to a task does not think (the alpha's acks took 30-150 s, F-8).
        think_next = not hand_off
        hand_off_note: Message | None = Message.user(HAND_OFF_NOTE, name="plan") if hand_off else None
        steps_judged = 0  # how many of watch.steps this loop has already looked at

        # Resume: an assistant message with tool calls that never got their results.
        run_messages = await self._store.list_run_messages(run.id)
        pending = _pending_tool_calls(run_messages)
        if pending is not None:
            assistant, calls = pending
            results = await self._execute_tool_calls(run, assistant, calls, ctl, watch, resumed=True)
            messages.extend(results)

        while True:
            self._check_cancel(ctl)
            if run.kind is RunKind.TASK:
                await self._hold(run, ctl, watch)
            if reason := self._supervisor.budget_exceeded(run, watch):
                await self._finish(run, ctl, messages, summary=reason)
                return

            # Anything Arsen said while this was running. It joins in HIS voice, as an ordinary
            # user message, so the model treats it exactly like the one that started the run and
            # it stays in the conversation afterwards. Appended, never inserted: the cached
            # prefix in front of it survives.
            for said in ctl.take_steers():
                # A cut-in on a call is spoken too: it wears the run's channel.
                messages.append(await self._persist(run, Message.user(said, channel=run.channel)))
                await emit(RunSteered(run_id="", conversation_id="", text=said[:400]))
                think_next = True  # Arsen just changed the task; work it out properly

            # Plan progress is the one thing that changes every step; it rides as the LAST message
            # so every cached token before it stays valid (the system prompt never changes).
            # It must exist at most ONCE. Removing it only when it was still last was wrong for
            # every step that called a tool — the assistant message and the tool results land
            # after it — so each such step left its block behind and the model read a stack of
            # plan blocks with a different step marked "current" in each.
            if plan_trailer is not None:
                messages[:] = [m for m in messages if m is not plan_trailer]
            plan_trailer = self._context.plan_message(run.plan) if run.plan is not None else None
            if hand_off_note is not None and not watch.steps and not watch.answer_only:
                # The pre-flight judged this work for a task: said where a plan would be, once.
                plan_trailer = hand_off_note
            if watch.answer_only:
                plan_trailer = Message.user(HANDED_OFF_NOTE, name="plan")
            # Tool results from earlier steps have been acted on; keep their head only. The DB
            # keeps the full text. Without this a 49-event calendar_list rode along in every one
            # of 8 model calls and a single scheduled run cost 217k prompt tokens (2026-09-05).
            _compress_old_tool_results(messages, view.results_tokens, chars_per_token=self._context.chars_per_token)
            # And whatever the step holds, it must fit the lane: a step that read 31 mails at once
            # (2026-09-16, the AI Newsletter: 222k chars of fresh results in one step) is beyond
            # any budget and was refused by the server outright. The largest fresh results give
            # up their tails first, only as much as the window demands.
            if view.window is not None:
                limit_tokens = (
                    view.window
                    - (lane_spec.max_tokens or _DEFAULT_MAX_TOKENS)
                    - self._settings().context_reserve_tokens
                )
                _fit_to_window(messages, run.id, int(limit_tokens * self._context.chars_per_token) - view.tools_chars)
            if plan_trailer is not None:
                messages.append(plan_trailer)
            run.steps_used += 1
            # `think_off_once` is set when a previous step spent its whole output allowance on
            # reasoning and wrote nothing: this step gets the allowance for the answer instead.
            # Otherwise `think_next` decides, and `run.think` (which may be None) is left alone
            # so the role's own configuration still has the last word on thinking at all.
            mechanical = self._settings().adaptive_thinking and not think_next
            adapter = self._adapters(
                main_role,
                think=False if think_off_once or mechanical else run.think,
                think_level=None if think_off_once or mechanical else run.think_level,
            )
            think_off_once = False
            think_next = False
            await emit(
                ModelCall(
                    run_id="",
                    conversation_id="",
                    role=main_role.value,
                    provider=adapter.spec.provider.value,
                    model=adapter.spec.model,
                    message_count=len(messages),
                    tool_count=len(tools),
                    think=adapter.spec.think,
                    think_level=adapter.spec.think_level,
                    context=context_breakdown(
                        messages,
                        run.id,
                        tools_chars=view.tools_chars,
                        window=view.window,
                        chars_per_token=self._context.chars_per_token,
                    ),
                )
            )
            sent_chars = prompt_chars(messages, view.tools_chars)
            try:
                text, reasoning, raw_calls, usage, finish = await self._stream(adapter, messages, tools, run, ctl)
            except ModelError as exc:
                # The lane is unreachable (booting, restarted, gone): the other lane serves the
                # same model. One try, shown in the inspector as a second model.call; a failure
                # there is the run's failure.
                other = self._settings().other_lane(main_role) if self._settings().lane_failover else None
                if not exc.retryable or other is None:
                    raise
                log.warning(
                    "run %s: lane %s unreachable (%s); falling over to %s", run.id, main_role.value, exc, other.value
                )
                adapter = self._adapters(
                    other, think=adapter.spec.think, think_level=adapter.spec.think_level, exact=True
                )
                await emit(
                    ModelCall(
                        run_id="",
                        conversation_id="",
                        role=f"{other.value} (failover from {main_role.value})",
                        provider=adapter.spec.provider.value,
                        model=adapter.spec.model,
                        message_count=len(messages),
                        tool_count=len(tools),
                        think=adapter.spec.think,
                        think_level=adapter.spec.think_level,
                    )
                )
                text, reasoning, raw_calls, usage, finish = await self._stream(adapter, messages, tools, run, ctl)
            self._context.observe(sent_chars, usage.prompt_tokens)
            calls = [self._policy.resolve(c) for c in raw_calls]  # facade op → canonical namespace.op
            if watch.answer_only and calls:
                # The work went to a task: this turn's only job left is the one-line answer. The
                # tools stay in the request (dropping them would re-read the whole prompt), and a
                # call made anyway is not run - the task does the work, not this turn.
                log.info("run %s: %d call(s) after handing off were not run", run.id, len(calls))
                calls = []
            run.usage = run.usage.add(usage)
            await emit(
                ModelDone(run_id="", conversation_id="", usage=usage, finish_reason=finish, tool_call_count=len(calls))
            )
            # A tool call can run out of output room too: its arguments arrive half-written and
            # vLLM still says finish_reason=tool_calls. Review 2026-10-22, World news: two calls
            # carrying a whole HTML page stopped at exactly 16,384 tokens, "not valid JSON" sent
            # the model guessing at an Outlook size limit, and the draft was never made.
            if finish == "length" or usage.completion_tokens >= (adapter.spec.max_tokens or _DEFAULT_MAX_TOKENS):
                spent = f"{usage.completion_tokens:,} output tokens"
                calls = [
                    c.model_copy(update={"arguments": {**c.arguments, "__cut_off__": spent}})
                    if "__raw__" in c.arguments
                    else c
                    for c in calls
                ]

            # Running out of output room is not the same as having nothing to say, and the two
            # used to be one branch. Measured 2026-09-06 on "HTML презентация за българското
            # население": two steps spent the whole 16,384-token allowance on reasoning and wrote
            # nothing, and the run died with "model returned an empty reply twice" after 22
            # minutes and 382k tokens — a diagnosis that was simply untrue.
            if finish == "length" and not calls:
                spent = f"{usage.completion_tokens:,} output tokens"
                if not text.strip() and reasoning.strip() and not watch.thinking_off_retry:
                    watch.thinking_off_retry = True
                    think_off_once = True
                    await emit(
                        GuardArmed(
                            run_id="",
                            conversation_id="",
                            guard="reasoning_used_the_allowance",
                            detail=f"{spent}, all of it reasoning, no answer",
                        )
                    )
                    messages.append(
                        await self._persist(
                            run,
                            Message.user(
                                f"[supervisor] Your last turn used its entire output allowance ({spent}) on "
                                "reasoning and produced no answer. Thinking is OFF for your next turn: write "
                                "the answer directly, and if it is long, start with the part that matters most.",
                                name="supervisor",
                            ),
                        )
                    )
                    await emit(
                        GuardConsumed(
                            run_id="", conversation_id="", guard="reasoning_used_the_allowance", detail="thinking off"
                        )
                    )
                    continue
                if text.strip() and not watch.continued:
                    # The answer is real but was cut mid-sentence. It is already in `messages`
                    # below, so the model can see its own partial text and carry on from it.
                    watch.continued = True
                    await emit(
                        GuardArmed(
                            run_id="", conversation_id="", guard="answer_truncated", detail=f"cut off at {spent}"
                        )
                    )
                    messages.append(
                        await self._persist(run, Message.assistant(text, reasoning=reasoning or None, partial=True))
                    )
                    messages.append(
                        await self._persist(
                            run,
                            Message.user(
                                "[supervisor] That answer was cut off when it ran out of output room. Carry on "
                                "from exactly where you stopped — do not repeat what you have already written.",
                                name="supervisor",
                            ),
                        )
                    )
                    await emit(
                        GuardConsumed(
                            run_id="", conversation_id="", guard="answer_truncated", detail="asked to continue"
                        )
                    )
                    await self._store.save_run(run)
                    continue

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
                raise RuntimeError(
                    "the model wrote nothing twice in a row "
                    f"(last turn: {usage.completion_tokens:,} output tokens, "
                    f"{len(reasoning):,} characters of reasoning, finish_reason={finish!r})"
                )

            assistant = await self._persist(run, Message.assistant(text, reasoning=reasoning or None, tool_calls=calls))
            messages.append(assistant)
            await self._store.save_run(run)

            if not calls:
                if run.plan is not None and run.plan.current_index is not None and not watch.plan_nudged:
                    # Final answer with open plan steps: one nudge (never a loop), then accept.
                    watch.plan_nudged = True
                    think_next = True  # a nudge is a change of course, not a mechanical step
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
                                "with jarvis.plan_step_done, or skip them explicitly, then answer. If your answer "
                                "asks Arsen for a decision, a confirmation or information the next steps need, you "
                                "are waiting for Arsen: leave those steps open, do nothing in his place, and end "
                                "with that question again.",
                                name="supervisor",
                            ),
                        )
                    )
                    await emit(GuardConsumed(run_id="", conversation_id="", guard="open_plan", detail="nudged"))
                    continue
                # Said while these very words were being written. A steer is taken at the next
                # step, and a run that answers has no next step - so the last thing Arsen said
                # was dropped, and on a call that is the whole second half of "he did not hear
                # me and repeated himself" (2026-09-19). The run answers again instead.
                if late := ctl.take_steers():
                    for said in late:
                        messages.append(await self._persist(run, Message.user(said, channel=run.channel)))
                        await emit(RunSteered(run_id="", conversation_id="", text=said[:400]))
                    if run.channel is Channel.VOICE:
                        # On a call that answer was never read out: he went on talking first, and
                        # answering the two halves one after the other is what he heard as Jarvis
                        # "answering each command separately" (2026-10-14).
                        messages.append(
                            await self._persist(
                                run,
                                Message.user(
                                    "[supervisor] He went on talking before that answer was read out, so he has "
                                    "not heard it. Answer everything he has said in one spoken reply.",
                                    name="supervisor",
                                ),
                            )
                        )
                    think_next = True
                    continue
                await self._done(run, ctl, message_id=assistant.id)
                # The knowledge graph learns from every ordinary exchange; an incognito chat is
                # the one place it must not look. Decided in code, not in the prompt.
                if (
                    self._learner is not None
                    and run.kind in _CONTEXT_KINDS
                    and self._settings().kg_learning
                    and not incognito
                ):
                    # A spoken reply that no tool result stands behind is Jarvis talking from memory:
                    # not a source the knowledge graph may learn a fact from - only his words are
                    # (alpha N-2: a wrong peak said on a call was learned as known context).
                    sourced = run.channel is not Channel.VOICE or bool(watch.steps)
                    self._learner.schedule(run.conversation_id, run.input_text, text if sourced else "", assistant.id)
                # A run that fought a site and won leaves a playbook behind for the next one.
                if self._reflector is not None and not incognito:
                    self._reflector.schedule(watch.steps, on_learned=self._note_playbook(run))
                return

            results = await self._execute_tool_calls(run, assistant, calls, ctl, watch)
            messages.extend(results)
            if watch.stop_now is not None:
                # No wrap-up: what is left to say is that it could not go on, and why.
                # Named: the core's line, not an answer - a task's report says why it stopped once.
                stopped = await self._persist(
                    run, Message.assistant(f"I stopped here: {watch.stop_now}.", name="stopped")
                )
                await self._done(run, ctl, message_id=stopped.id, summary=watch.stop_now)
                return
            if watch.answer_only and run.plan is not None and run.plan.current_index is not None:
                # A plan made before the hand-off is not this turn's work any more: the task's.
                for s in run.plan.steps:
                    if s.status in (PlanStepStatus.PENDING, PlanStepStatus.IN_PROGRESS):
                        s.status = PlanStepStatus.SKIPPED
                await self._store.save_run(run)
            # Something went wrong, or the plan just moved on: the next step has a real decision
            # to make rather than another result to act on.
            fresh = watch.steps[steps_judged:]
            steps_judged = len(watch.steps)
            think_next = (
                think_next
                or any(s.kind is ToolResultKind.ERROR for s in fresh)
                or any(c.name in _PLAN_TOOLS for c in calls)
            ) and not watch.answer_only  # a one-line answer needs no reasoning

            verdict = await self._supervisor.review(run, watch, messages, emit, ctl.cancel)
            if verdict is None:
                continue
            if verdict.verdict == "stop":
                await self._finish(run, ctl, messages, summary=f"stopped by supervisor: {verdict.reason}")
                return
            if verdict.verdict == "nudge":
                think_next = True  # it has been told to change approach; let it reason about how
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
        except (ModelCancelled, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and not ctl.cancel.is_set():
                # The core is shutting down, nobody cancelled the run: it is interrupted and
                # resumes after the restart. Turned into a cancel here, a redeploy in the middle
                # of a step read as "cancelled by Arsen" (alpha 2026-10-30, F-6).
                raise
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
        results: list[Message] = []
        # Consecutive read-only calls go out together; everything else keeps its place in the
        # order the model asked for, so a mutation is never overtaken by a read that followed
        # it. Grouping (rather than hoisting every read to the front) is what makes that safe.
        for group in _dispatch_groups(calls, self._can_parallelise, run):
            self._check_cancel(ctl)
            if len(group) == 1:
                message, step = await self._execute_one(run, group[0], ctl, watch, resumed=resumed)
                results.append(message)
                if step is not None:
                    watch.steps.append(step)
                continue
            sem = asyncio.Semaphore(max(1, self._settings().max_parallel_tools))
            # return_exceptions so a failure cannot leave siblings running unobserved; the
            # first one is re-raised once they have all settled.
            settled = await asyncio.gather(
                *(self._execute_guarded(run, c, ctl, watch, sem, resumed=resumed) for c in group),
                return_exceptions=True,
            )
            for outcome in settled:
                if isinstance(outcome, BaseException):
                    raise outcome
            # Appended in the order the model asked for, not the order they came back, so the
            # transcript and the supervisor's view of the run stay deterministic.
            for message, step in cast("list[tuple[Message, StepRecord | None]]", settled):
                results.append(message)
                if step is not None:
                    watch.steps.append(step)
        return results

    async def _execute_guarded(
        self,
        run: Run,
        call: ToolCall,
        ctl: RunControl,
        watch: RunWatch,
        sem: asyncio.Semaphore,
        *,
        resumed: bool,
    ) -> tuple[Message, StepRecord | None]:
        async with sem:
            return await self._execute_one(run, call, ctl, watch, resumed=resumed)

    def _can_parallelise(self, call: ToolCall, run: Run) -> bool:
        """A call that only reads, needs no confirmation, and claims no idempotency key."""
        if call.name in _PLAN_TOOLS:
            return False
        spec = self._registry.get(call.name)
        if spec is None or not spec.read_only:
            return False
        return not self._settings().confirmations.needs_confirmation(
            call.name, destructive=bool(spec.destructive), unattended=run.kind in _UNATTENDED
        )

    async def _execute_one(
        self,
        run: Run,
        call: ToolCall,
        ctl: RunControl,
        watch: RunWatch,
        *,
        resumed: bool,
    ) -> tuple[Message, StepRecord | None]:
        """One tool call, from policy to persisted result. The StepRecord is what the
        supervisor sees; None means this call is not a step it should judge."""
        emit = ctl.emitter.emit
        if call.name in _PLAN_TOOLS:
            return await self._plan_tool(run, call, emit), None
        spec = self._registry.get(call.name)
        read_only = spec.read_only if spec else False
        key = f"{run.id}:{call.id}"
        if resumed and not read_only:
            # We do not know whether the mutation happened. Ask, unless already answered. An
            # approval given BEFORE the call went out does not answer that: it said "do it", not
            # "it did not happen" - taken as the answer, a mail approved and sent just before a
            # restart went out twice (found building delegated tasks, 2026-10-29).
            decision = await self._recorded_decision(run, call.id, after_dispatch=True)
            if decision is None:
                await self._wait_for_confirmation(
                    run,
                    ctl,
                    call,
                    reason="interrupted during this action — did it complete? Approve to run it again, reject to skip.",
                )
                decision = await self._recorded_decision(run, call.id, after_dispatch=True)
            if decision is not None and not decision[0]:
                message = await self._tool_message(
                    run, call, ToolResult.failure("skipped by user after interruption"), 0, emit
                )
                return message, None
            key = f"{key}:retry"

        error = self._registry.validate(call.name, call.arguments)
        if spec is not None and run.channel is Channel.VOICE and not self._settings().voice.allows(spec.namespace):
            # Before the argument check: what the model needs to hear is "not on a call", not
            # which field it forgot in a call it should not have made at all.
            error = (
                f"{call.name} is not among the tools allowed on a call (settings.voice.namespaces). Tell Arsen "
                "in one sentence that you can do it in the chat."
            )
        if error is None and spec is not None and is_memory_writer(spec) and await self._is_incognito(run):
            # Hidden from the list, but a model can still name a tool it was not offered.
            error = (
                f"{call.name} is not available in an incognito chat: nothing from this conversation "
                "is stored anywhere. Tell Arsen you cannot save it here."
            )
        refused = (
            await self._tasks.refusal(run, call.name, steered_by_other=ctl.steered_by_other)
            if self._tasks is not None
            else None
        )
        error = refused or error
        if error is not None and run.kind is RunKind.TASK and refused is None:
            await self._end_if_unreachable(run, call, watch)
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
            return (
                await self._tool_message(run, call, result, 0, emit),
                step_of(call.name, call.arguments, result),
            )

        # A call that has already failed the same way twice is not tried a third time. The
        # supervisor used to catch this at three, and its remedy was to end the run: on
        # 2026-09-07 `onenote_create` returned (-2147213311) at 12:06:46, 12:08:08 and
        # 12:08:53 and the run was stopped at 12:08:55 with nothing delivered. Refusing here
        # costs no tool call and no judge call, and leaves the model a turn to route around it.
        if repeat := _repeated_failure(watch, call):
            result = ToolResult.failure(repeat)
            return (
                await self._tool_message(run, call, result, 0, emit),
                step_of(call.name, call.arguments, result),
            )

        # Sending: anyone not on the approved list gets a draft instead. This rewrites the
        # call before it is dispatched, so the model cannot talk its way past it.
        # Outlook on a host (`<host>.outlook_send`) and the core's own IMAP/SMTP mail (`mail.send`) alike.
        is_send = call.name.split(".")[-1] == "outlook_send" or call.name == "mail.send"
        if is_send and not call.arguments.get("draft"):
            blocked = self._settings().email.unapproved(
                str(call.arguments.get("to", "")), str(call.arguments.get("cc", ""))
            )
            if blocked:
                call.arguments["draft"] = True
                call.arguments["_policy_note"] = f"not on the approved-direct-send list: {', '.join(blocked)}"
                log.info("email policy: drafting instead of sending to %s", ", ".join(blocked))

        confirmations = self._settings().confirmations
        unattended = run.kind in _UNATTENDED
        confirm = confirmations.needs_confirmation(
            call.name, destructive=bool(spec and spec.destructive), unattended=unattended
        )
        reason = "this action cannot be undone"
        if run.kind is RunKind.TASK:
            # Nobody is watching a task, so its own rule replaces the chat's: whatever the
            # confirmation mode and always_allow say, anything not read-only waits for him, unless
            # he freed it by name (settings.tasks.no_approval). Not the tool's own `destructive`
            # flag: the browser marks clicking Send as harmless (docs/stories/13_delegated_tasks.md).
            confirm = self._settings().tasks.needs_approval(call.name, read_only=read_only)
            reason = "a delegated task asks before anything that is not read-only"
        elif not confirm and confirmations.checks_outbound(call.name, unattended=unattended):
            # Whatever the mode: something said or promised to others in his name that he did
            # not ask for waits for him (features/outbound.py).
            outbound = await self._unasked_outbound(run, call)
            if outbound is not None:
                confirm, reason = True, outbound
        if confirm and run.channel is Channel.VOICE:
            # On a call nobody can tap a card at the ear: the model asks aloud, and the same call
            # goes through once he has said something after the question (whether that was a yes
            # is the model's to judge - it heard him; no word lists).
            refusal = await self._voice_confirmation(run, call)
            if refusal is not None:
                return await self._tool_message(run, call, refusal, 0, emit), None
        elif confirm:
            decision = await self._recorded_decision(run, call.id)
            if decision is None and run.kind is RunKind.TASK and (refused := await self._denied_before(run, call)):
                # Arsen already said no to exactly this - same tool, same arguments. A task does not
                # ask him again; the no goes back to it as a decision (alpha N-4).
                return await self._tool_message(run, call, ToolResult.failure(refused), 0, emit), None
            if decision is None:
                await self._wait_for_confirmation(run, ctl, call, reason=reason)
                decision = await self._recorded_decision(run, call.id)
            if decision is not None and not decision[0]:
                note = f" ({decision[1]})" if decision[1] else ""
                message = await self._tool_message(run, call, ToolResult.failure(f"rejected by user{note}"), 0, emit)
                return message, None

        if not read_only and not await self._store.claim_idempotency(key, run.id, call.name):
            # The action already ran under this key, so answer with what it RETURNED.
            # `record_idempotent_result` has always stored that and nothing ever read it
            # back: the model was told "duplicate call refused", which reads as a failure,
            # and a model that believes its mail was not sent sends it again another way.
            stored = await self._store.idempotent_result(key)
            result = _replay(stored) or ToolResult.failure(
                "duplicate call refused (this key already ran and recorded no result)"
            )
            return await self._tool_message(run, call, result, 0, emit), None

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
        policy_note = call.arguments.pop("_policy_note", None)
        if self._shadow is not None and not await self._is_incognito(run):
            self._shadow.outgoing(call.name, call.arguments, ref=key, source=run.kind.value)
        t0 = time.perf_counter()
        result = await self._registry.call(
            call.name,
            call.arguments,
            cancel=ctl.cancel,
            idempotency_key=key,
            timeout_s=self._tool_timeout(call),
        )
        if policy_note and result.kind is not ToolResultKind.ERROR:
            note = f"\n[saved as a draft: {policy_note}]"
            result = result.model_copy(update={"text": result.text + note})
        if result.kind is ToolResultKind.ERROR and run.kind is RunKind.TASK:
            await self._end_if_unreachable(run, call, watch)
        duration = int((time.perf_counter() - t0) * 1000)
        # A wait paused the world on purpose: credit the run clock so the time budget measures
        # work, not waiting. (browser.wait is a wait too — recognised by name.)
        if (call.name in IS_WAIT_TOOL or call.name.endswith(".wait")) and result.kind is not ToolResultKind.ERROR:
            watch.paused_s += time.perf_counter() - t0
        if not read_only:
            await self._store.record_idempotent_result(key, result.model_dump_json())
        if call.name.startswith("browser."):
            result = await self._with_site_playbook(result, watch)
        if result.ends_turn and result.kind is not ToolResultKind.ERROR:
            # The work went to a task: what is left of this turn is the one-line answer.
            watch.answer_only = True
        step = step_of(call.name, call.arguments, result)
        shown = _already_in_context(watch, step, result, read_only=read_only)
        return await self._tool_message(run, call, shown or result, duration, emit), step

    async def _end_if_unreachable(self, run: Run, call: ToolCall, watch: RunWatch) -> None:
        """A task's call failed and the machine behind it is down: the task ends here, honestly,
        instead of wrapping up around a hole. Decided from the provider's status, never from the
        wording of the error."""
        down = self._registry.down(call.name.split(".", 1)[0])
        if down is None or self._tasks is None or watch.stop_now is not None:
            return
        error, since = down
        tz = ZoneInfo(self._settings().timezone)
        namespace = call.name.split(".", 1)[0]
        reason = f"{namespace} is not reachable (down since {since.astimezone(tz).strftime('%H:%M')}: {error})"
        # Only what the brief needs ends the task: a search engine that is out while fetch can
        # still read the pages is a failed call to work around, not the end (alpha N-3).
        if await self._tasks.unreachable(run, namespace, reason):
            watch.stop_now = reason

    async def _hold(self, run: Run, ctl: RunControl, watch: RunWatch) -> None:
        """A task holds still between its steps while Arsen is on a call (the voice comes first on
        the shared GPU), and while as many other tasks are working as may at once - a task let go
        by an approval does not run beside the one that took its place (F-7). The held time is not
        work, so the time budget does not count it, and a cancel still lands at once."""
        gate = self._tasks
        if gate is None or not gate.should_hold(run):
            return
        t0 = time.perf_counter()
        await gate.paused(run, True)
        try:
            while gate.should_hold(run):
                self._check_cancel(ctl)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ctl.cancel.wait(), timeout=_CALL_HOLD_POLL_S)
            self._check_cancel(ctl)
        finally:
            watch.paused_s += time.perf_counter() - t0
            await gate.paused(run, False)

    async def _with_site_playbook(self, result: ToolResult, watch: RunWatch) -> ToolResult:
        """Arriving on a site Jarvis has a playbook for: the playbook rides on the tool result,
        once per run. This is where a learned lesson meets the next run — before its first click,
        without Arsen having to mention the site by name."""
        if self._skills is None or result.kind is ToolResultKind.ERROR:
            return result
        m = _URL_IN_RESULT.search(result.text or "")
        if not m:
            return result
        try:
            found = await self._skills.store.for_site(m.group(0))
        except Exception as exc:  # a broken skill file is not the run's problem
            log.warning("site playbook lookup failed: %s", exc)
            return result
        fresh = [(n, b) for n, b in found if n not in watch.playbooks_shown]
        if not fresh:
            return result
        blocks = []
        for name, body in fresh[:2]:
            watch.playbooks_shown.add(name)
            blocks.append(
                f"[Playbook `{name}` — learned on this site earlier; follow it before improvising]\n{body[:3000]}"
            )
        return result.model_copy(update={"text": result.text + "\n\n" + "\n\n".join(blocks)})

    def _note_playbook(self, run: Run) -> Callable[[str, str], Awaitable[None]]:
        async def note(name: str, description: str) -> None:
            await self._persist(
                run,
                Message.user(
                    f"[playbook] Learned `{name}` from this run: {description}. It will be shown automatically "
                    "the next time a browser tool lands on that site. Review or edit it under Skills.",
                    name="playbook",
                ),
            )

        return note

    async def _tool_message(
        self, run: Run, call: ToolCall, result: ToolResult, duration_ms: int, emit: Emit
    ) -> Message:
        await emit(
            ToolResultEvent(
                run_id="", conversation_id="", call_id=call.id, name=call.name, result=result, duration_ms=duration_ms
            )
        )
        text = result.to_model_text()
        if run.kind is RunKind.TASK:
            # The id task.finish cites as evidence, where the model reads the result (F-4: it
            # cited file names and object ids because the call's own id was nowhere to be seen).
            text = f"[ref {call.id}] {text}"
        message = await self._persist(run, Message.tool(call.id, call.name, text))
        if result.images and self._attachments is not None and message.id:
            # The picture becomes an attachment of the tool message: hydrated into the request
            # like any image Arsen sends, shown in the transcript, and gone with the conversation.
            ids: list[str] = []
            for img in result.images:
                try:
                    att = await self._attachments.add_file(
                        data=base64.b64decode(img.base64),
                        filename=img.name,
                        mime=img.mime,
                        conversation_id=run.conversation_id,
                    )
                    ids.append(att.id)
                except Exception as exc:
                    log.warning("tool image from %s not kept: %s", call.name, exc)
            if ids:
                try:
                    bound = await self._attachments.bind(
                        ids, message_id=message.id, conversation_id=run.conversation_id
                    )
                    # Hydrated NOW, not only when a later run rebuilds the history: this message
                    # goes to the model on the very next step, and it must carry the picture.
                    for att in bound:
                        att.data_url = await self._attachments.data_url(att.id)
                    message.attachments = bound
                except Exception as exc:
                    log.warning("tool image from %s not bound: %s", call.name, exc)
        # What the model reads from here on: whole up to the admission limit, else its head and
        # the marker with the ref. The DB has the whole text; the view is what costs tokens.
        return admit(message, self._admit_chars)

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
        await ctl.emitter.announce_activity()  # the sidebar dot turns from "working" to "asks you"
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
        await ctl.emitter.announce_activity()  # ...and back to "working"

    async def _voice_confirmation(self, run: Run, call: ToolCall) -> ToolResult | None:
        """None when this call may run now; otherwise what the model is told instead.

        The first time a call that needs confirming is made in a conversation on a call, it is
        refused with "ask him". The same call (name and arguments) made again goes through only
        if he has said something since the question - in this run (a steer) or in a later one.
        "Since" is by message, not by clock: two messages can share a timestamp."""
        key = (run.conversation_id, call.name, json.dumps(call.arguments, sort_keys=True, default=str))
        now = datetime.now(UTC)
        self._voice_asks = {k: v for k, v in self._voice_asks.items() if now - v[0] < _VOICE_ASK_TTL}
        said = [
            m.id
            for m in await self._store.list_messages(run.conversation_id, limit=60)
            if m.role is Role.USER and m.name is None
        ]
        last = said[-1] if said else None
        asked = self._voice_asks.get(key)
        if asked is not None:
            if last != asked[1]:
                del self._voice_asks[key]
                return None
            return ToolResult.failure("he has not answered yet: wait for his reply before doing it")
        self._voice_asks[key] = (now, last)
        return ToolResult.failure(
            "needs his spoken go-ahead first: say in one sentence exactly what you are about to do (what, "
            f"to whom) and ask him. If he says yes, call {call.name} again with exactly the same arguments; "
            "if he says no or changes it, do not."
        )

    async def _unasked_outbound(self, run: Run, call: ToolCall) -> str | None:
        """Why this call must wait for him, or None (features/outbound.py). The classifier runs
        on the run's lane, like every role inside a run."""
        history = await self._store.list_messages(run.conversation_id, limit=40)
        return await unasked_outbound(
            self._adapters(RoleName.CLASSIFIER, kind=run.kind),
            call,
            user=self._settings().user_name,
            request=run.input_text,
            history=[m for m in history if m.run_id != run.id or m.role is Role.USER],
            run_messages=await self._store.list_run_messages(run.id),
        )

    async def _denied_before(self, run: Run, call: ToolCall) -> str | None:
        """Why this exact call (tool and arguments) was already turned down in this conversation,
        or None. Compared as data: the same name and the same arguments, nothing interpreted."""
        wanted = (call.name, json.dumps(call.arguments, sort_keys=True, default=str))
        asked: dict[str, tuple[str, str]] = {}
        for earlier in await self._store.list_runs(run.conversation_id, limit=50):
            for ev in await self._store.list_events(earlier.id):
                cid = str(ev.get("call_id") or "")
                if ev.get("type") == "tool.confirm_requested" and cid != call.id:
                    asked[cid] = (
                        str(ev.get("name")),
                        json.dumps(ev.get("arguments") or {}, sort_keys=True, default=str),
                    )
                elif ev.get("type") == "tool.confirm_resolved" and not ev.get("approved") and asked.get(cid) == wanted:
                    note = f" ({ev['note']})" if ev.get("note") else ""
                    return (
                        f"Arsen already turned down exactly this {call.name} call in this task{note}. Do not ask for "
                        "it again: do it another way if there is one, or say in your report that it was not done "
                        "because he declined."
                    )
        return None

    async def _recorded_decision(
        self, run: Run, call_id: str, *, after_dispatch: bool = False
    ) -> tuple[bool, str | None] | None:
        """Arsen's last answer about this call. ``after_dispatch``: only one given after the call
        went out (its tool.call event), i.e. an answer to "did it complete?"."""
        decision: tuple[bool, str | None] | None = None
        for ev in await self._store.list_events(run.id):
            if ev.get("call_id") != call_id:
                continue
            if ev.get("type") == "tool.call" and after_dispatch:
                decision = None  # what was said before it went out does not count
            elif ev.get("type") == "tool.confirm_resolved":
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

    def _tool_timeout(self, call: ToolCall) -> float:
        """The core's deadline for one tool call.

        A tool that takes its own ``timeout_s`` (shell_run, a 5-minute report script) knows how
        long its work takes; the core honours that up to ``settings.tool_timeout_max_s`` and adds
        a little slack so the tool's own timeout fires first and reports properly. Otherwise the
        default applies. Before this every call was cut at 120 s regardless (2026-09-05).
        """
        limit = float(self._settings().tool_timeout_max_s)
        asked = call.arguments.get("timeout_s")
        if isinstance(asked, int | float) and asked > 0:
            return min(float(asked) + 10.0, limit)
        # A deliberate wait is not a runaway tool — it is meant to take exactly this long, so
        # its own duration governs the deadline instead of tool_timeout_max_s.
        if call.name in IS_WAIT_TOOL:
            secs = call.arguments.get("seconds")
            if isinstance(secs, int | float) and secs > 0:
                return float(secs) + 30.0
        return min(_TOOL_TIMEOUT_S, limit)

    # --- finishing -----------------------------------------------------------------

    async def _finish(self, run: Run, ctl: RunControl, messages: list[Message], *, summary: str) -> None:
        """End a run that ran out of budget or was stopped — with the work, not just the verdict.

        This used to write "I stopped here: <reason>. Last progress: <400 chars>" and stop, which
        is a dead end: of 81 runs on 2026-09-07, 15 ended that way (9 supervisor, 6 budget) and
        Arsen was left re-typing "продължи" with the stop text quoted back. Everything needed for
        an answer is already in the conversation, so we spend one more call writing it: no tools,
        so it cannot start anything new, and thinking off, so the whole allowance goes to the
        answer. If that call fails we still write the old note rather than nothing.
        """
        note = f"[stopped: {summary}]"
        answer = await self._wrap_up(run, ctl, messages, summary=summary)
        if run.kind is RunKind.TASK:
            # A task's report says why it stopped on its own; its answer is the substance only,
            # never the supervisor's words or the model's "now let me fetch..." (F-5).
            text = answer or f"I stopped here: {summary}."
        elif answer:
            text = f"{answer}\n\n{note}"
        else:
            text = f"I stopped here: {summary}."
            last = next((m for m in reversed(messages) if m.role is Role.ASSISTANT and m.content.strip()), None)
            if last is not None:
                text += f"\n\nLast progress: {last.content.strip()[:400]}"
        # A task's line of its own when nothing was written is the core's, not an answer: named,
        # so its report says why it stopped once, not twice (alpha F-5).
        named = "stopped" if run.kind is RunKind.TASK and not answer else None
        msg = await self._persist(run, Message.assistant(text, name=named))
        await self._done(run, ctl, message_id=msg.id, summary=summary)

    async def wrap_up_cancelled(self, run: Run, ctl: RunControl) -> str | None:
        """A delegated task cancelled while working still says what it got to: the same wrap-up a
        budget stop gets, with the cancel as the reason, on a fresh control so the cancel that
        ended the work does not end the answer too (alpha F-16). The id of the answer, or None."""
        if run.kind is not RunKind.TASK:
            return None
        view = await self._context.assemble_view(run)
        text = await self._wrap_up(
            run, RunControl(emitter=ctl.emitter), view.messages, summary="cancelled by Arsen", finish=False
        )
        if not text:
            return None
        return (await self._persist(run, Message.assistant(text))).id

    async def _wrap_up(
        self, run: Run, ctl: RunControl, messages: list[Message], *, summary: str, finish: bool = True
    ) -> str:
        """One tool-less, thinking-off pass that turns what the run gathered into an answer."""
        ask = Message.user(
            f"[supervisor] This run is over: {summary}. You get no more tool calls. Write the answer "
            "now, from what is already in this conversation: what you found, what you did, and what "
            "is left undone. Give Arsen the substance you gathered, not a status report. If a step "
            "failed, say which and why in one line. Do not promise to continue.",
            name="supervisor",
        )
        finish_spec = self._registry.get("task.finish") if run.kind is RunKind.TASK and finish else None
        if finish_spec is not None:
            # A delegated task ends with its outcome, not only with prose: what the log shows for
            # each DoD item is what the report verifies (F-5: a complete answer was "not done"
            # with every item "not reported" because the budget ended it before task.finish).
            ask = Message.user(
                f"[supervisor] This task's run is over: {summary}. Call task.finish now: a summary of "
                "what you found and did (the substance, for Arsen, in English) and every DoD item by "
                "number - met or not, with the [ref ...] ids of the tool results that show it. No "
                "other tool. Do not promise to continue.",
                name="supervisor",
            )
        try:
            adapter = self._adapters(self._settings().lane_for(run.kind), think=False, think_level=None)
            offered = [finish_spec] if finish_spec is not None else []
            text, _reasoning, calls, usage, _finish = await self._stream(adapter, [*messages, ask], offered, run, ctl)
            run.usage = run.usage.add(usage)
            await self._store.save_run(run)
            ends = [c for c in calls if finish_spec is not None and c.name == finish_spec.name][:1]
            if ends:
                call = ends[0]
                messages.append(await self._persist(run, Message.assistant(text, tool_calls=[call])))
                result, _step = await self._execute_one(run, call, ctl, RunWatch(budget=run.budget), resumed=False)
                messages.append(result)
                return str(call.arguments.get("summary") or text).strip()
            return text.strip()
        except RunCancelledError:
            raise
        except Exception as exc:
            log.warning("could not write a closing answer for %s: %s", run.id, exc)
            return ""

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

    async def _follow_up_skills(self, run: Run, detected: list[str], trace: dict[str, object]) -> list[str]:
        """The previous turn's skills for a follow-up, ahead of what the detector found.

        "само прати мейла, вече имаш всичко" is too short to name a skill, so the detector either
        finds none or guesses from "мейл" - email_triage, not the digest runbook the previous turn
        ran on - and the model went at the job without its instructions (2026-10-02). A short
        message is taken as a follow-up and keeps the previous turn's skills first; a long one
        keeps them only when the detector found nothing. At most three ride.
        """
        carried = last_turn_skills(await self._store.list_messages(run.conversation_id), run.id)
        if not carried:
            return detected
        if len(run.input_text.strip()) <= _FOLLOW_UP_CHARS:
            names = [*carried, *(d for d in detected if d not in carried)][:3]
        elif not detected:
            names = carried[:3]
        else:
            return detected
        if names != detected:
            trace["carried"] = carried
        return names

    async def _persist(self, run: Run, message: Message) -> Message:
        message.conversation_id = run.conversation_id
        message.run_id = run.id
        # A voice run's own messages - the reply, a cut-in - are spoken; a context or summary
        # note is neither and keeps "text", so the transcript marks only what was actually said.
        if run.channel is Channel.VOICE and message.name is None and message.role in (Role.USER, Role.ASSISTANT):
            message.channel = Channel.VOICE
        await self._store.add_message(message)
        # An answer (an assistant message with no tool calls) carries the files the run attached.
        if message.role is Role.ASSISTANT and not message.tool_calls and self._attachments is not None and message.id:
            files = self._attachments.take_outgoing(run.id)
            if files:
                message.attachments = await self._attachments.bind(
                    files, message_id=message.id, conversation_id=run.conversation_id
                )
        # Conversation-level, so it bypasses the run emitter's seq and is not persisted twice.
        self._bus.publish(MessageCreated(message=message))
        return message

    @staticmethod
    def _check_cancel(ctl: RunControl) -> None:
        if ctl.cancel.is_set():
            raise RunCancelledError(None)


_PLAN_TOOLS = {spec.name for spec in PLAN_TOOLS}
#: Said where a plan would be, on the first step of a turn the pre-flight judged as work for a task.
HAND_OFF_NOTE = (
    "[core note] This request was judged as longer work for a background task, whatever language it is "
    "in: call tasks.delegate now - a short English title is enough, his words and this chat go with it - "
    "then answer him in one line. Do not start the work yourself in this turn: no searching, no reading "
    "pages. Answer instead only if something it needs is not connected (say so), or a fact only he knows "
    "is missing (ask the one question)."
)
#: The trailer once the work is handed off: the turn's last step is the answer.
HANDED_OFF_NOTE = (
    "[core note] The work is with the task now. Answer Arsen in one short line: that you took it and "
    'what will come back. It has not been done yet - do not say or begin with "done". No more tools in '
    "this turn."
)
#: How long a question asked aloud on a call ("shall I send it?") waits for its answer.
_VOICE_ASK_TTL = timedelta(minutes=15)
# Two identical failures are a pattern; a third is a waste of a tool call and of the run.
_SAME_FAILURE_LIMIT = 2


def _dispatch_groups(
    calls: list[ToolCall], parallelisable: Callable[[ToolCall, Run], bool], run: Run
) -> list[list[ToolCall]]:
    """Split a batch into runs of calls that may go out together, in the model's own order.

    Consecutive read-only calls form one group; anything else is a group of its own. Order is
    preserved, so a write is never overtaken by a read the model put after it. Measured over
    the whole run history 2026-09-07: 253 batches held more than one call (one held 26), and
    running the read-only ones concurrently recovers 362 s of the 1,301 s they spent serialized.
    """
    groups: list[list[ToolCall]] = []
    for call in calls:
        if parallelisable(call, run) and groups and parallelisable(groups[-1][-1], run):
            groups[-1].append(call)
        else:
            groups.append([call])
    return groups


_URL_IN_RESULT = re.compile(r"""https?://[^\s"'<>)\]]+""")


# A result shorter than this is cheaper to repeat than to point at.
_REPEAT_POINTER_MIN_CHARS = 600


def _already_in_context(watch: RunWatch, step: StepRecord, result: ToolResult, *, read_only: bool) -> ToolResult | None:
    """A pointer instead of a second copy when a read-only call returns exactly what the same call
    already returned in this run. 7 days to 2026-09-28: 1,135 of 5,655 calls repeated an identical
    earlier call - 412 of them browser.read {"mode": "text"} on an unchanged page, each re-adding up
    to 16k chars of context. The step record (and the DB event) keep the real result."""
    if not read_only or result.kind is not ToolResultKind.DATA or result.images:
        return None
    if len(result.text) < _REPEAT_POINTER_MIN_CHARS:
        return None
    for n, earlier in enumerate(watch.steps, 1):
        if earlier.args_hash == step.args_hash and earlier.result_hash == step.result_hash:
            return ToolResult.data(
                f"[identical to the result of step {n} ({step.tool}, same arguments): nothing changed since, and "
                "that result is already in your context above. Use it instead of reading again; if you are "
                "waiting for a change, act first or use jarvis.wait_until.]"
            )
    return None


def _repeated_failure(watch: RunWatch, call: ToolCall) -> str | None:
    """The refusal text when this exact call has already failed this way, or None."""
    digest = args_hash(call.name, call.arguments)
    failures = [s for s in watch.steps if s.args_hash == digest and s.kind is ToolResultKind.ERROR]
    if len(failures) < _SAME_FAILURE_LIMIT:
        return None
    return (
        f"not run: this exact {call.name} call already failed {len(failures)} times with the same "
        f"error, so it will not be retried. Last error: {failures[-1].summary[:200]}. "
        "Do it a different way, or say what you have and what is blocked."
    )


_CHARS_PER_TOKEN = 3.2
# Come back to this fraction of the budget, not merely under it. Rewriting a message the model
# has already been sent is the one thing that costs a FULL re-prefill: everything after it has
# to be read again, and prefill is a fixed ~1,200 tok/s. Measured on ardi 2026-09-06 — trimming
# just enough put the run back over the line on the very next step, so it fired again and again:
# 64 s, then 59 s, then 49 s inside a single run, each one re-reading ~78k tokens. Crossing the
# line rarely and coming back well under it pays for the crossing.
# Ageing rewrites messages in the middle of the prompt, and the prefix cache is gone for the
# rest of that request; so when it has to happen it goes deep, to half the budget, and then not
# again for a long while — every-step ageing was the 2026-09-17 newsletter's undoing.
_COMPRESS_TO = 0.5
# What vLLM applies when a request names no max_tokens (the model's generation_config).
_DEFAULT_MAX_TOKENS = 16_384


def _fit_to_window(messages: list[Message], run_id: str, limit_chars: int) -> None:
    """Cut this step's fresh tool results, largest first and only by the overflow, so the whole
    prompt fits the lane's window. Ageing (above) is a budget the run lives within; this is the
    wall it must not hit. The DB keeps every byte; each cut result becomes a view naming its refs."""
    total = sum(len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) for m in messages)
    overflow = total - limit_chars
    if overflow <= 0:
        return
    fresh = sorted(
        (i for i, m in enumerate(messages) if m.role is Role.TOOL and m.run_id == run_id),
        key=lambda i: -len(messages[i].content),
    )
    for i in fresh:
        if overflow <= 0:
            break
        m = messages[i]
        room = len(m.content) - AGED_VIEW_CHARS  # what this one can give up: down to the small view
        if room <= 0:
            continue
        cut = min(room, overflow)
        shrunk = view_of(m, len(m.content) - cut)
        overflow -= len(m.content) - len(shrunk.content)
        messages[i] = shrunk


def _compress_old_tool_results(
    messages: list[Message], budget_tokens: int, *, chars_per_token: float = _CHARS_PER_TOKEN
) -> None:
    """Keep this run's tool results under a token budget by truncating the OLDEST first.

    Nothing is touched while the results fit: a "read 26 mails and summarise" run must keep the
    bodies it is aggregating (blanket truncation made the model re-read the same mails until the
    supervisor stopped it, 2026-09-05). Once over budget, the oldest results shrink to a head,
    never the most recent step's, and only down to ``_COMPRESS_TO`` of the budget so the next
    step does not trip it again. The DB always keeps the full text; replacement by copy keeps
    earlier context snapshots intact.
    """
    last_assistant = max((i for i, m in enumerate(messages) if m.role is Role.ASSISTANT), default=-1)
    if last_assistant < 0:
        return
    budget = int(budget_tokens * chars_per_token)
    tool_idx = [i for i, m in enumerate(messages) if m.role is Role.TOOL]
    total = sum(len(messages[i].content) for i in tool_idx)
    if total <= budget:
        return
    target = int(budget * _COMPRESS_TO)
    for i in tool_idx:
        if i >= last_assistant:
            break  # the current step's results are what the model is answering
        m = messages[i]
        shrunk = aged(m)
        if shrunk is m:
            continue  # short, or already the small view
        messages[i] = shrunk
        total -= len(m.content) - len(shrunk.content)
        if total <= target:
            break


def _replay(stored: str | None) -> ToolResult | None:
    """The result a duplicate call already produced, labelled as a replay. None if unusable."""
    if not stored:
        return None
    try:
        result = ToolResult.model_validate_json(stored)
    except ValidationError:
        return None
    note = "\n[this exact call already ran in this run; the result above is what it returned then]"
    return result.model_copy(update={"text": result.text + note})


def _pending_tool_calls(run_messages: list[Message]) -> tuple[Message, list[ToolCall]] | None:
    if not run_messages:
        return None
    last_assistant = next((m for m in reversed(run_messages) if m.role is Role.ASSISTANT), None)
    if last_assistant is None or not last_assistant.tool_calls:
        return None
    answered = {m.tool_call_id for m in run_messages if m.role is Role.TOOL}
    pending = [c for c in last_assistant.tool_calls if c.id not in answered]
    return (last_assistant, pending) if pending else None
