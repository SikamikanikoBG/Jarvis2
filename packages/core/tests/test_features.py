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


async def test_a_centred_graph_always_contains_its_centre(harness: Harness):
    """`graph(center=X)` is what the UI draws a ring around, so X has to be in it.

    The node set was a plain `set` and the trim was `list(ids)[:limit]` — no order at all, so
    for any entity with more neighbours than the limit the centre usually fell outside the slice
    and the ring was drawn around nothing. Trimming now drops the outside of the neighbourhood.
    """
    core = harness.core
    hub = await core.knowledge.upsert("ArDi", type="thing", summary="the always-on box")
    for i in range(30):
        other = await core.knowledge.upsert(f"Service {i:02d}", type="thing")
        await core.knowledge.add_edge(hub.id, other.id, "runs")

    for limit in (1, 5, 12, 80):
        g = await core.knowledge.graph(center=hub.id, depth=1, limit=limit)
        assert hub.id in {n.id for n in g.nodes}, f"centre dropped at limit={limit}"
        assert len(g.nodes) <= limit
        # Every edge drawn connects two nodes that were actually returned.
        node_ids = {n.id for n in g.nodes}
        assert all(e.src in node_ids and e.dst in node_ids for e in g.edges)
    # Same question, same picture.
    twice = [[n.id for n in (await core.knowledge.graph(center=hub.id, depth=1, limit=7)).nodes] for _ in range(3)]
    assert twice[0] == twice[1] == twice[2]


async def test_remembering_a_relation_with_no_target_stores_no_relation(harness: Harness):
    """`kg.remember` used to upsert an entity literally named "?" for an empty `to` and wire the
    fact to it — a node in Arsen's graph that means nothing, from a model that half-answered."""
    core = harness.core
    res = await core.registry.call(
        "kg.remember",
        {
            "name": "Vader",
            "summary": "The 3x3090 box",
            "type": "thing",
            "relations": [{"to": "", "relation": "hosts"}, {"to": "vLLM", "relation": "hosts"}],
        },
        cancel=asyncio.Event(),
        idempotency_key="t1",
    )
    assert res.kind.value != "error"
    assert await core.knowledge.find("?") is None
    vader = await core.knowledge.find("Vader")
    assert vader is not None
    detail = await core.knowledge.detail(vader.id)
    assert detail is not None and [(e.relation, e.other.name) for e in detail.edges] == [("hosts", "vLLM")]


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
    # The skill body rides in the persisted per-turn context message (after the input), not in
    # the system prompt - so the stable prefix stays cacheable across turns.
    msgs = harness.chat.calls[-1][0]
    assert "three sections" not in msgs[0].content
    ctx = [m for m in msgs if m.role.value == "user" and m.name == "context"]
    assert len(ctx) == 1 and "three sections" in ctx[0].content and ctx[0].content.startswith("[Context")
    # It is persisted, so the next turn replays the same tokens at the same place.
    stored = await core.store.list_messages(conv.id)
    assert [m.name for m in stored if m.role.value == "user"] == [None, "context"]
    assert (await core.store.get_conversation(conv.id)).preview != ctx[0].content[:160]  # type: ignore[union-attr]
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
    # The plan block is the LAST message (ephemeral trailer), never in the system prompt, so the
    # cached tool results before it stay valid step after step.
    plan_calls = harness.chat.calls[2:]
    assert all("## Plan" not in call[0][0].content for call in plan_calls)
    assert any(call[0][-1].name == "plan" and "## Plan" in call[0][-1].content for call in plan_calls)
    # The trailer is not persisted.
    assert not any(m.name == "plan" for m in await core.store.list_messages(conv.id))
    # ...and exactly ONE of it exists per call. The trailer was only popped when it was still
    # the last message, but a step that calls a tool puts the assistant message and the tool
    # results after it, so every such step left its block behind: the model ended up reading
    # several plan blocks, each with a different step marked "← current".
    for i, call in enumerate(plan_calls):
        blocks = [m for m in call[0] if m.name == "plan"]
        assert len(blocks) == 1, f"call {i} carried {len(blocks)} plan blocks, not 1"
        assert call[0][-1] is blocks[0], f"call {i} did not end with the plan block"
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


async def test_run_now_appears_in_the_schedule_history(harness: Harness):
    """"Run now" is a fire like any other and must be recorded as one.

    It called fire_now without claiming a slot or recording it, so the run existed but the
    schedule never heard about it: the card still said "last run: <the previous scheduled one>"
    next to a run that had just finished, and the history list stayed empty.
    """
    core = harness.core
    harness.chat.push(FakeTurn(text="done now"))
    sched = await core.schedules.create(name="Weekly report", prompt="write it", cron="0 9 * * 1", tz="UTC")
    assert await core.schedules.fires(sched.id) == []

    run_id, conv_id = await core.scheduler.run_now(sched)
    fires = await core.schedules.fires(sched.id)
    assert [(f.run_id, f.conversation_id) for f in fires] == [(run_id, conv_id)]
    after = await core.schedules.get(sched.id)
    assert after is not None and after.last_run_id == run_id
    # The recurring schedule is untouched by a manual run: it still fires on its own cron.
    assert after.enabled and after.next_fire is not None and after.next_fire > datetime.now(UTC)
    assert after.next_fire == sched.next_fire


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
    run, _ = await core.engine.create_run(text="every weekday at 8:30 prep my standup notes", conversation_id=conv.id)
    # Creating a standing automation asks first (it is destructive); Arsen approves.
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert req.name == "schedule.create"
    await core.engine.confirm(run.id, req.call_id, True)
    seen += await harness.wait_for(sub, "run.done")
    assert any(e.type == "schedule.changed" for e in seen)
    items = await core.schedules.list()
    assert [s.name for s in items] == ["Standup"] and items[0].cron == "30 8 * * 1-5" and items[0].next_fire is not None


# --- confirmations ---------------------------------------------------------------------------


def test_confirmation_policy():
    """Arsen asked to be able to switch the human confirmation off; unattended runs never ask,
    and outlook_send keeps asking unless it is unattended."""
    from jarvis_proto.settings import Confirmations

    default = Confirmations()
    assert default.needs_confirmation("workocholic.shell_run", destructive=True, unattended=False)
    assert not default.needs_confirmation("workocholic.outlook_list", destructive=False, unattended=False)
    # A schedule firing at 06:30 has nobody to answer it.
    assert not default.needs_confirmation("workocholic.shell_run", destructive=True, unattended=True)

    off = Confirmations(mode="off")
    assert not off.needs_confirmation("workocholic.shell_run", destructive=True, unattended=False)
    # ...but the always_ask floor still holds for sending mail.
    assert off.needs_confirmation("workocholic.outlook_send", destructive=True, unattended=False)
    assert not off.needs_confirmation("workocholic.outlook_send", destructive=True, unattended=True)

    selective = Confirmations(always_allow=["workocholic.shell_run", "fs.*"])
    assert not selective.needs_confirmation("workocholic.shell_run", destructive=True, unattended=False)
    assert not selective.needs_confirmation("fs.write", destructive=True, unattended=False)
    assert selective.needs_confirmation("workocholic.calendar_create", destructive=True, unattended=False)


async def test_confirmations_off_runs_a_destructive_tool_without_asking(harness: Harness):
    from tests.test_loop import with_tools

    tools = await with_tools(harness)
    harness.enable(confirmations=__import__("jarvis_proto").Confirmations(mode="off"))
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="test.send", arguments={})]),
        FakeTurn(text="done, no questions asked"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="send it", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    assert tools.calls == ["send"]
    assert not any(e.type == "tool.confirm_requested" for e in seen)


# --- email policy ----------------------------------------------------------------------------


def test_email_policy_matching():
    from jarvis_proto.settings import EmailPolicy

    p = EmailPolicy(approved_direct_send=["aapostolov@postbank.bg", "@smartlab.bg"])
    assert p.unapproved("aapostolov@postbank.bg") == []
    assert p.unapproved("Someone <x@smartlab.bg>") == []
    assert p.unapproved("rumen@bank.bg") == ["rumen@bank.bg"]
    # Mixed lists: only the unapproved ones are named, separators are both , and ;
    assert p.unapproved("aapostolov@postbank.bg, rumen@bank.bg; ceo@bank.bg") == ["rumen@bank.bg", "ceo@bank.bg"]
    # cc counts too
    assert p.unapproved("aapostolov@postbank.bg", "boss@bank.bg") == ["boss@bank.bg"]
    # An empty list drafts everything - the safe default for a fresh install.
    assert EmailPolicy().unapproved("anyone@anywhere.com") == ["anyone@anywhere.com"]
    assert EmailPolicy(allow_any_recipient=True).unapproved("anyone@anywhere.com") == []


async def test_unapproved_recipient_becomes_a_draft(harness: Harness):
    """The rewrite happens before dispatch, so the model cannot talk its way past it."""
    from pydantic import BaseModel

    from jarvis_core.tools import BuiltinProvider, tool
    from jarvis_proto import ToolResult
    from jarvis_proto.settings import EmailPolicy

    class _SendArgs(BaseModel):
        to: str
        subject: str = ""
        body: str = ""
        cc: str = ""
        draft: bool = False

    class MailHost(BuiltinProvider):
        name = "laptop"

        def __init__(self) -> None:
            self.sent: list[dict] = []
            super().__init__()

        @tool("laptop.outlook_send", description="send", args=_SendArgs, destructive=True)
        async def _send(
            self, to: str, subject: str = "", body: str = "", cc: str = "", draft: bool = False
        ) -> ToolResult:
            self.sent.append({"to": to, "cc": cc, "draft": draft})
            return ToolResult.data("drafted" if draft else "sent")

    host = MailHost()
    harness.core.registry.add(host)
    await harness.core.registry.refresh()
    harness.enable(email=EmailPolicy(approved_direct_send=["boss@bank.bg"]))

    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="c1", name="laptop.outlook_send", arguments={"to": "stranger@x.com", "subject": "hi"})
            ]
        ),
        FakeTurn(text="drafted it"),
    )
    await harness.core.engine.create_run(text="mail the stranger", conversation_id=conv.id, kind=RunKind.SCHEDULED)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    assert host.sent == [{"to": "stranger@x.com", "cc": "", "draft": True}]
    result = next(e for e in seen if e.type == "tool.result")
    assert "saved as a draft" in result.result.text and "stranger@x.com" in result.result.text

    # An approved recipient really goes out.
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="c2", name="laptop.outlook_send", arguments={"to": "boss@bank.bg", "subject": "hi"})
            ]
        ),
        FakeTurn(text="sent"),
    )
    await harness.core.engine.create_run(text="mail the boss", conversation_id=conv.id, kind=RunKind.SCHEDULED)
    await harness.wait_for(sub, "run.done", timeout=10)
    assert host.sent[-1] == {"to": "boss@bank.bg", "cc": "", "draft": False}


# --- scheduled-run framing + notify -----------------------------------------------------------


async def test_scheduled_run_knows_it_is_the_reminder(harness: Harness):
    """Firing 'Remind Arsen to ...' must not create a second schedule: the system prompt tells
    the model it IS the scheduled prompt, running now."""
    core = harness.core
    harness.chat.push(FakeTurn(text="Reminder: go approve the Jira items."))
    from datetime import UTC, datetime, timedelta

    sched = await core.schedules.create(
        name="Daily Approvals",
        prompt="Remind Arsen to approve Jira items",
        at=datetime.now(UTC) - timedelta(minutes=1),
        tz="UTC",
    )
    assert await core.scheduler.tick() == 1
    fires = await core.schedules.fires(sched.id)
    conv = await core.store.get_conversation(fires[0].conversation_id)  # type: ignore[arg-type]
    assert conv is not None
    # wait for the run to finish
    async with asyncio.timeout(10):
        while (await core.store.get_run(fires[0].run_id)).status is not RunStatus.DONE:  # type: ignore[union-attr, arg-type]
            await asyncio.sleep(0.05)
    system = harness.chat.calls[-1][0][0].content
    assert "## This run" in system and "Daily Approvals" in system and "firing NOW" in system
    assert "Do not create, edit or re-schedule" in system
    # A plain chat run gets no such framing.
    harness.chat.push(FakeTurn(text="hi"))
    c2 = await core.store.create_conversation()
    sub = harness.subscribe(c2.id)
    await core.engine.create_run(text="hello", conversation_id=c2.id)
    await harness.wait_for(sub, "run.done")
    assert "## This run" not in harness.chat.calls[-1][0][0].content


async def test_schedule_create_asks_for_confirmation_in_chat(harness: Harness):
    """Creating a standing automation is consequential: interactive runs must confirm it."""
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(
                    id="c1", name="schedule.create", arguments={"name": "X", "prompt": "do x", "cron": "0 9 * * *"}
                )
            ]
        ),
        FakeTurn(text="ok, not created"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="remind me daily", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert req.name == "schedule.create"
    await harness.core.engine.confirm(run.id, req.call_id, False, "no")
    await harness.wait_for(sub, "run.done")
    assert [s.name for s in await harness.core.schedules.list()] == []


async def test_notify_discord_posts_and_reports_honestly(harness: Harness):
    import httpx

    from jarvis_core.features.notify import NotifyTools, _split

    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(__import__("json").loads(request.read()))
        return httpx.Response(204)

    tools = NotifyTools(lambda: harness.core.settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    # Unconfigured → a clear error, not a fake success.
    res = await tools.call("notify.discord", {"text": "hi"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    assert res.kind.value == "error" and "no Discord webhook" in res.text
    harness.enable(discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    res = await tools.call(
        "notify.discord", {"text": "Approve the Jira items"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5
    )
    assert res.kind.value == "data" and posted == [{"content": "Approve the Jira items"}]
    # Long text is split on line boundaries under Discord's limit.
    parts = _split("line\n" * 1000, 1900)
    assert len(parts) >= 3 and all(len(p) <= 1900 for p in parts)

    # A failing webhook is reported with the status, never swallowed.
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    tools2 = NotifyTools(lambda: harness.core.settings, client=httpx.AsyncClient(transport=httpx.MockTransport(bad)))
    res = await tools2.call("notify.discord", {"text": "x"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    assert res.kind.value == "error" and "429" in res.text
