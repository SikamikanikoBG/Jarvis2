"""Phase 2 features through the real Core: boards, knowledge, skills, compaction, planner, schedules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools.facades import ExposurePolicy, build_facades
from jarvis_proto import RunKind, RunStatus, ToolCall, ToolSpec
from tests.conftest import Harness

# --- facades ------------------------------------------------------------------------------


def _spec(name: str, *, read_only: bool = True, props: dict | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} does things",
        input_schema={
            "type": "object",
            "properties": props or {"x": {"type": "string"}},
            "required": list((props or {"x": 1}).keys()),
        },
        read_only=read_only,
        provider=name.split(".", maxsplit=1)[0],
    )


def test_facades_are_derived_from_the_live_list_and_resolve_back():
    specs = [_spec(f"homelab.t{i}") for i in range(5)] + [_spec("jarvis.time"), _spec("fetch.fetch")]
    exposed, _facades = build_facades(specs)
    names = {t.name for t in exposed}
    assert names == {"homelab", "jarvis.time", "fetch.fetch"}  # small namespaces stay flat
    facade = next(t for t in exposed if t.name == "homelab")
    assert facade.input_schema["properties"]["op"]["enum"] == [f"t{i}" for i in range(5)]
    assert "t3(x: string)" in facade.description
    policy = ExposurePolicy(mode="facade")
    policy.expose(specs)
    call = policy.resolve(ToolCall(id="c", name="homelab", arguments={"op": "t2", "args": {"x": "1"}}))
    assert call.name == "homelab.t2" and call.arguments == {"x": "1"}
    flat = policy.resolve(ToolCall(id="c", name="homelab", arguments={"op": "t1", "x": "2"}))
    assert flat.name == "homelab.t1" and flat.arguments == {"x": "2"}
    bad = policy.resolve(ToolCall(id="c", name="homelab", arguments={"op": "nope"}))
    assert bad.name == "homelab.nope"  # → unknown tool error from the registry, not a crash
    assert policy.resolve(ToolCall(id="c", name="jarvis.time", arguments={})).name == "jarvis.time"


def test_auto_mode_switches_on_threshold():
    specs = [_spec(f"a.t{i}") for i in range(5)]
    p = ExposurePolicy(mode="auto", threshold=12)
    assert len(p.expose(specs)) == 5 and not p.active
    p.threshold = 3
    assert len(p.expose(specs)) == 1 and p.active


# --- boards -------------------------------------------------------------------------------


async def test_boards_tools_and_context(harness: Harness):
    core = harness.core
    b = await core.boards.create_board("Priorities")
    await core.boards.add_note(b.id, "Ship V2 phase 2")
    harness.chat.push(
        FakeTurn(
            tool_calls=[ToolCall(id="c1", name="notes.add", arguments={"board": "Priorities", "text": "Call Rumen"})]
        ),
        FakeTurn(text="pinned"),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="remember to call Rumen", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert any(e.type == "board.changed" for e in seen)
    notes = await core.boards.list_notes(b.id)
    assert [n.text for n in notes] == ["Ship V2 phase 2", "Call Rumen"]
    # The board is in the system prompt of the model call.
    system = harness.chat.calls[-1][0][0].content
    assert "## Notes boards" in system and "Ship V2 phase 2" in system
    # notes.add on an unknown board creates it.
    await core.registry.refresh()
    res = await core.registry.call(
        "notes.add", {"board": "New", "text": "x"}, cancel=asyncio.Event(), idempotency_key="k"
    )
    assert "Pinned" in res.text and await core.boards.find_board("New") is not None


# --- knowledge ----------------------------------------------------------------------------


async def test_knowledge_context_uses_literal_matches_and_learner_upserts(harness: Harness):
    core = harness.core
    rumen = await core.knowledge.upsert(
        "Rumen Petrov", type="person", summary="Head of retail banking", aliases=["Rumen"]
    )
    proj = await core.knowledge.upsert("Project Phoenix", type="project", summary="Core banking migration")
    await core.knowledge.add_edge(rumen.id, proj.id, "sponsors")
    block = await core.knowledge.context_block("what did Rumen say about the budget?")
    assert block and "Rumen Petrov" in block and "sponsors" in block
    assert await core.knowledge.context_block("nothing relevant here") is None

    # Learner: scripted extraction on the classifier/fake adapter after a chat run.
    harness.enable(kg_learning=True)
    harness.chat.push(
        FakeTurn(text="Maria Ivanova leads the mobile app team and reports to Rumen Petrov."),
        FakeTurn(
            text='{"entities":[{"name":"Maria Ivanova","type":"person","summary":"Leads the mobile app team","aliases":["Maria"]}],'
            '"relations":[{"from":"Maria Ivanova","to":"Rumen Petrov","relation":"reports to"}]}'
        ),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(
        text="who leads the mobile app team and who do they report to at the bank?", conversation_id=conv.id
    )
    await harness.wait_for(sub, "run.done")
    await core.learner.wait()
    maria = await core.knowledge.find("Maria")
    assert maria is not None and maria.type == "person"
    detail = await core.knowledge.detail(maria.id)
    assert detail is not None and any(e.relation == "reports to" for e in detail.edges)
    merged = await core.knowledge.merge(maria.id, rumen.id)
    assert merged is not None and "Maria Ivanova" in merged.aliases


# --- skills -------------------------------------------------------------------------------


async def test_skills_detection_structural_then_model_and_injection(harness: Harness):
    core = harness.core
    await core.skills.put(
        "weekly_status",
        "---\ndescription: How to write the weekly status report\ntriggers: [weekly status]\n---\n# Weekly status\nUse the template with three sections.",
    )
    await core.skills.put("deck", "---\ndescription: Building slide decks\n---\nUse 16:9.")
    # Structural trigger → no model call; the skill body lands in the system prompt.
    harness.chat.push(FakeTurn(text="ok"))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="please draft the weekly status for Rumen", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    names = next(e for e in seen if e.type == "context.skills").names
    assert names == ["weekly_status"]
    assert "three sections" in harness.chat.calls[-1][0][0].content
    # Disabled skills are never detected.
    await core.skills.set_enabled("weekly_status", False)
    assert [s.enabled for s in await core.skills.list() if s.name == "weekly_status"] == [False]
    assert await core.skills.body("deck") == "Use 16:9."


# --- compaction ---------------------------------------------------------------------------


async def test_compaction_summarises_older_turns_once(harness: Harness):
    core = harness.core
    s = core.settings.model_copy(deep=True)
    s.history_token_budget = 120  # ~380 chars
    core.apply_settings(s)
    conv = await core.store.create_conversation()
    for i in range(6):
        await core.store.add_message(
            __import__("jarvis_proto").Message.user(f"user message number {i} " + "x" * 60, conversation_id=conv.id)
        )
        await core.store.add_message(
            __import__("jarvis_proto").Message.assistant(f"reply {i} " + "y" * 60, conversation_id=conv.id)
        )
    harness.chat.push(FakeTurn(text="- decisions so far: none\n- open: reply to 5"), FakeTurn(text="final"))
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="continue", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    latest = await core.compactor.latest(conv.id)
    assert latest is not None and "open: reply to 5" in latest[1]
    # The chat call saw the summary message and not the oldest turns.
    msgs = harness.chat.calls[-1][0]
    assert any(m.name == "summary" for m in msgs)
    assert not any("user message number 0" in m.content for m in msgs)


# --- planner ------------------------------------------------------------------------------


async def test_multi_step_request_gets_a_plan_and_steps_advance(harness: Harness):
    core = harness.core
    harness.chat.push(
        FakeTurn(text='{"tier": "multi_step"}'),  # preflight (classifier)
        FakeTurn(text='{"goal": "Weekly report", "steps": ["Collect numbers", "Write the report"]}'),  # planner
        FakeTurn(
            tool_calls=[
                ToolCall(id="p1", name="jarvis.plan_step_done", arguments={"index": 1, "note": "numbers collected"})
            ]
        ),
        FakeTurn(text="Here is the report."),  # answers with step 2 open → one nudge
        FakeTurn(tool_calls=[ToolCall(id="p2", name="jarvis.plan_step_done", arguments={"index": 2})]),
        FakeTurn(text="Here is the report, final."),
    )
    harness.enable(planning_enabled=True)
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(
        text="Collect this week's numbers from the homelab and write the weekly report for Rumen with three sections.",
        conversation_id=conv.id,
    )
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    types = [e.type for e in seen]
    assert "plan.created" in types and types.count("plan.step_done") == 2
    assert any(e.type == "guard.armed" and e.guard == "open_plan" for e in seen)
    done = await core.store.get_run(run.id)
    assert done is not None and done.plan is not None and all(s.status.value == "done" for s in done.plan.steps)
    # The plan block was in the system prompt of the model calls.
    assert any("## Plan" in call[0][0].content for call in harness.chat.calls[2:])
    # Plan tools were exposed only because a plan exists.
    assert any(t.name == "jarvis.plan_step_done" for t in harness.chat.calls[2][1])


async def test_simple_request_skips_planning(harness: Harness):
    harness.enable(planning_enabled=True)
    harness.chat.push(FakeTurn(text="4"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="what is 2+2", conversation_id=conv.id)  # < 25 chars: no preflight call
    seen = await harness.wait_for(sub, "run.done")
    assert not any(e.type == "plan.created" for e in seen)
    assert len(harness.chat.calls) == 1


# --- schedules ----------------------------------------------------------------------------


async def test_schedule_fires_into_its_own_conversation_once(harness: Harness):
    core = harness.core
    harness.chat.push(FakeTurn(text="Good morning brief."), FakeTurn(text="again?"))
    past = datetime.now(UTC) - timedelta(minutes=1)
    sched = await core.schedules.create(
        name="Morning brief", prompt="Give me the morning brief", at=past, tz="Europe/Sofia"
    )
    global_sub = harness.subscribe("none")
    # Ticking twice must fire exactly once (UNIQUE slot) and disable the one-shot.
    fired = await core.scheduler.tick()
    fired2 = await core.scheduler.tick()
    assert (fired, fired2) == (1, 0)
    fires = await core.schedules.fires(sched.id)
    assert len(fires) == 1 and fires[0].conversation_id and fires[0].run_id
    conv = await core.store.get_conversation(fires[0].conversation_id)
    assert (
        conv is not None
        and conv.kind.value == "scheduled"
        and conv.folder_key == sched.id
        and conv.folder_label == "Morning brief"
    )
    assert (await core.schedules.get(sched.id)).enabled is False
    # The sidebar learns about the new conversation from the broadcast conversation.updated.
    seen_conv = False
    async with asyncio.timeout(5):
        while True:
            run = await core.store.get_run(fires[0].run_id)
            while not global_sub.queue.empty():
                ev = global_sub.queue.get_nowait()
                if getattr(ev, "type", "") == "conversation.updated" and ev.conversation.id == conv.id:
                    seen_conv = True
            if run is not None and run.status is RunStatus.DONE and run.kind is RunKind.SCHEDULED:
                break
            await asyncio.sleep(0.05)
    assert seen_conv
    msgs = await core.store.list_messages(conv.id)
    assert msgs[-1].content == "Good morning brief."


async def test_cron_schedule_skip_policy_advances_without_firing(harness: Harness):
    core = harness.core
    sched = await core.schedules.create(name="Hourly", prompt="tick", cron="0 * * * *", tz="UTC", catch_up="skip")
    # Pretend the slot was 3 hours ago (laptop was asleep).
    old = datetime.now(UTC) - timedelta(hours=3)
    await core.db.execute("UPDATE schedules SET next_fire = ? WHERE id = ?", (old.isoformat(), sched.id))
    assert await core.scheduler.tick() == 0
    s = await core.schedules.get(sched.id)
    assert s is not None and s.next_fire is not None and s.next_fire > datetime.now(UTC)
    assert await core.schedules.fires(sched.id) == []


async def test_schedule_tool_creates_from_chat(harness: Harness):
    core = harness.core
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="schedule.create",
                    arguments={"name": "Standup", "prompt": "Prep standup notes", "cron": "30 8 * * 1-5"},
                )
            ]
        ),
        FakeTurn(text="scheduled"),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="every weekday at 8:30 prep my standup notes", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert any(e.type == "schedule.changed" for e in seen)
    items = await core.schedules.list()
    assert [s.name for s in items] == ["Standup"] and items[0].cron == "30 8 * * 1-5" and items[0].next_fire is not None
