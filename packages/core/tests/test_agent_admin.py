"""Jarvis reading and changing its own settings, schedules and routes (issue #14,
docs/stories/14_agent_admin.md). Driven through the real loop with scripted model turns, so the
approvals, proposals and the trail are what a run would really produce."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jarvis_core.app import create_app
from jarvis_core.features.admin import ChangeRefused, SettingsConflict, SettingsInvalid, Who
from jarvis_core.models.base import ProbeResult
from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import ConversationKind, MailAccount, RunKind, RunStatus, ToolCall, ToolResult
from jarvis_proto.settings import Confirmations
from tests.conftest import Harness

ADMIN_TOOLS = (
    "settings.get",
    "settings.set",
    "settings.revert",
    "schedule.get",
    "schedule.update",
    "schedule.run_now",
    "schedule.fires",
    "jarvis.api_describe",
    "jarvis.api_read",
    "jarvis.api_write",
)


def call(name: str, args: dict[str, Any], cid: str = "c1") -> FakeTurn:
    return FakeTurn(tool_calls=[ToolCall(id=cid, name=name, arguments=args)])


async def run_chat(
    harness: Harness,
    *turns: FakeTurn,
    kind: RunKind = RunKind.CHAT,
    conv_kind: ConversationKind = ConversationKind.CHAT,
) -> tuple[Any, list[Any]]:
    """Run one turn to its end with the scripted model turns; approve nothing."""
    harness.chat.push(*turns, FakeTurn(text="done"))
    conv = await harness.core.store.create_conversation(kind=conv_kind)
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(text="please", conversation_id=conv.id, kind=kind)
    seen = await harness.wait_for(sub, "run.done")
    return run, seen


def tool_results(seen: list[Any]) -> list[Any]:
    return [e.result for e in seen if e.type == "tool.result"]


# --- settings.get ---------------------------------------------------------------------------


async def test_settings_get_index_section_and_leaf_with_meaning(harness: Harness):
    _run, seen = await run_chat(
        harness,
        call("settings.get", {}, "a"),
        call("settings.get", {"path": "voice"}, "b"),
        call("settings.get", {"path": "voice.max_turn_s"}, "c"),
    )
    index, section, leaf = (r.text for r in tool_results(seen))
    assert "voice:" in index and len(index) <= 2_500 and "Dry, quick" not in index  # no persona text
    assert "- max_turn_s = 600" in section and "hold music" in section
    assert "voice.max_turn_s = 600" in leaf and "default 600" in leaf and "hold music" in leaf


async def test_settings_get_masks_secrets_unless_revealed_and_a_typo_gets_a_hint(harness: Harness):
    s = harness.core.settings.model_copy(
        update={"mail_accounts": [MailAccount(address="a@gmail.com", app_password="abcdefghijklmnop")]}
    )
    harness.core.apply_settings(s)
    _run, seen = await run_chat(
        harness,
        call("settings.get", {"path": "mail_accounts"}, "a"),
        call("settings.get", {"path": "mail_accounts[address=a@gmail.com].app_password", "reveal": True}, "b"),
        call("settings.get", {"path": "voice.max_turn"}, "c"),
    )
    masked, clear, typo = tool_results(seen)
    assert "abcdefghijklmnop" not in masked.text and "set (16 chars)" in masked.text
    assert "abcdefghijklmnop" in clear.text
    assert "did you mean 'voice.max_turn_s'" in typo.text


# --- settings.set ---------------------------------------------------------------------------


async def test_a_harmless_change_applies_at_once_with_a_trail_and_an_event(harness: Harness):
    core = harness.core
    run, seen = await run_chat(harness, call("settings.set", {"set": {"voice.hold_volume": 0.3}, "reason": "quieter"}))
    assert not any(e.type == "tool.confirm_requested" for e in seen)
    assert core.settings.voice.hold_volume == 0.3
    assert (await core.store.load_settings()).voice.hold_volume == 0.3
    [rec] = await core.admin.changes.list()
    assert rec.actor == "jarvis" and rec.run_id == run.id and rec.call_id == "c1" and rec.status == "applied"
    assert [(f.path, f.old, f.new) for f in rec.fields] == [("voice.hold_volume", 0.4, 0.3)]
    assert rec.reason == "quieter" and rec.revertible
    changed = [e for e in seen if e.type == "settings.changed"]
    assert changed and changed[0].paths == ["voice.hold_volume"] and changed[0].change_id == rec.id
    [result] = tool_results(seen)
    assert result.change["id"] == rec.id and "0.4 → 0.3" in result.text


async def test_a_deep_set_keeps_every_other_lane(harness: Harness):
    """merge_settings replaces dicts whole: a naive {"roles": {"chat": ...}} would drop the rest."""
    core = harness.core
    before = core.settings.model_dump(mode="json")["roles"]
    await run_chat(harness, call("settings.set", {"set": {"roles.chat.temperature": 0.2}}))
    after = core.settings.model_dump(mode="json")["roles"]
    assert after["chat"]["temperature"] == 0.2
    assert {k: v for k, v in after.items() if k != "chat"} == {k: v for k, v in before.items() if k != "chat"}
    assert {k: v for k, v in after["chat"].items() if k != "temperature"} == {
        k: v for k, v in before["chat"].items() if k != "temperature"
    }


async def test_an_invalid_value_changes_nothing_not_even_the_valid_half(harness: Harness):
    core = harness.core
    _run, seen = await run_chat(
        harness, call("settings.set", {"set": {"voice.hold_volume": 0.2, "voice.turn_threshold": 0.1}})
    )
    [result] = tool_results(seen)
    assert result.kind.value == "error" and "nothing was changed" in result.text and "turn_threshold" in result.text
    assert core.settings.voice.hold_volume == 0.4
    assert await core.admin.changes.list() == []
    assert not any(e.type == "settings.changed" for e in seen)


async def test_a_lane_endpoint_change_asks_with_the_diff_and_a_no_leaves_it(harness: Harness):
    core = harness.core
    harness.chat.push(call("settings.set", {"set": {"roles.chat.base_url": "fake://other"}}), FakeTurn(text="ok"))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="move the chat lane", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert "can cut Jarvis off" in req.reason and req.risk == ["lifeline"]
    assert [(f.path, f.new) for f in req.preview] == [("roles.chat.base_url", "fake://other")]
    await core.engine.confirm(run.id, req.call_id, False)
    await harness.wait_for(sub, "run.done")
    assert core.settings.roles["chat"].base_url == "fake://"


async def test_an_approved_lane_change_is_probed_saved_and_kept_after_its_recheck(harness: Harness):
    core = harness.core
    harness.chat.push(call("settings.set", {"set": {"roles.chat.base_url": "fake://other"}}), FakeTurn(text="ok"))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="move the chat lane", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    await core.engine.confirm(run.id, req.call_id, True)
    await harness.wait_for(sub, "run.done")
    assert core.settings.roles["chat"].base_url == "fake://other"
    [rec] = await core.admin.changes.list()
    assert rec.status == "probing"
    assert (await core.admin.recheck(rec.id)).status == "applied"  # type: ignore[union-attr]


async def test_a_lane_that_does_not_answer_is_refused_before_saving(harness: Harness):
    core = harness.core

    async def probe(spec: Any) -> ProbeResult:
        return ProbeResult(spec.base_url == "fake://", 1, "" if spec.base_url == "fake://" else "connection refused")

    core.admin.probe_spec = probe
    harness.enable(confirmations=Confirmations(mode="off", outbound_check=[]))
    _run, seen = await run_chat(harness, call("settings.set", {"set": {"roles.chat.base_url": "http://dead:1/v1"}}))
    [result] = tool_results(seen)
    assert result.kind.value == "error" and "did not answer" in result.text and "connection refused" in result.text
    assert core.settings.roles["chat"].base_url == "fake://"


async def test_a_lifeline_that_fails_its_recheck_puts_itself_back(harness: Harness):
    core = harness.core
    plan = core.admin.plan({"roles.background.base_url": "fake://bg"})
    rec = await core.admin.apply(plan, who=Who())
    assert rec is not None and rec.status == "probing"

    async def dead(spec: Any) -> ProbeResult:
        return ProbeResult(spec.base_url != "fake://bg", 1, "gone")

    core.admin.probe_spec = dead
    sub = harness.subscribe("none")
    await core.admin.recheck(rec.id)
    assert core.settings.roles["background"].base_url == "fake://"
    first = await core.admin.changes.get(rec.id)
    assert first is not None and first.status == "auto_reverted" and "gone" in first.note
    seen = []
    while not sub.queue.empty():
        seen.append(sub.queue.get_nowait())
    assert any(e.type == "settings.changed" for e in seen)


async def test_a_probing_change_is_rechecked_after_a_restart(harness: Harness):
    core = harness.core
    rec = await core.admin.apply(core.admin.plan({"roles.background.base_url": "fake://bg"}), who=Who())
    assert rec is not None
    await harness.restart()

    async def dead(spec: Any) -> ProbeResult:
        return ProbeResult(spec.base_url != "fake://bg", 1, "gone")

    harness.core.admin.probe_spec = dead
    await harness.core.admin.recover()
    async with asyncio.timeout(5):
        while (await harness.core.admin.changes.get(rec.id)).status == "probing":  # type: ignore[union-attr]
            await asyncio.sleep(0.02)
    assert harness.core.settings.roles["background"].base_url == "fake://"


async def test_revert_restores_and_refuses_after_a_later_change(harness: Harness):
    core = harness.core
    a = await core.admin.apply(core.admin.plan({"voice.hold_volume": 0.1}), who=Who())
    assert a is not None
    back = await core.admin.revert(a.id, who=Who())
    assert core.settings.voice.hold_volume == 0.4 and back.reverts == a.id
    assert (await core.admin.changes.get(a.id)).status == "reverted"  # type: ignore[union-attr]
    with pytest.raises(ChangeRefused, match="nothing to revert"):
        await core.admin.revert(a.id, who=Who())
    b = await core.admin.apply(core.admin.plan({"voice.hold_volume": 0.2}), who=Who())
    await core.admin.apply(core.admin.plan({"voice.hold_volume": 0.3}), who=Who())
    assert b is not None
    with pytest.raises(ChangeRefused, match="has changed since"):
        await core.admin.revert(b.id, who=Who())
    assert core.settings.voice.hold_volume == 0.3


async def test_a_removed_list_item_is_a_delete_and_its_revert_puts_it_back(harness: Harness):
    core = harness.core
    core.apply_settings(
        core.settings.model_copy(
            update={"mail_accounts": [MailAccount(address="x@gmail.com", app_password="secretpass")]}
        )
    )
    plan = core.admin.plan(remove=["mail_accounts[address=x@gmail.com]"])
    risk, why = core.admin.assess_plan(plan)
    assert "delete" in risk and "removes something" in why
    rec = await core.admin.apply(plan, who=Who())
    assert rec is not None and core.settings.mail_accounts == []
    await core.admin.revert(rec.id, who=Who())
    assert core.settings.mail_accounts[0].app_password == "secretpass"


async def test_a_masked_secret_written_back_keeps_the_saved_one(harness: Harness):
    core = harness.core
    core.apply_settings(
        core.settings.model_copy(
            update={"mail_accounts": [MailAccount(address="x@gmail.com", app_password="secretpass")]}
        )
    )
    shown = core.settings.model_dump(mode="json")["mail_accounts"]
    shown[0]["app_password"] = "•••• set (10 chars)"
    shown[0]["enabled"] = False
    rec = await core.admin.apply(core.admin.plan({"mail_accounts": shown}), who=Who())
    assert rec is not None and [f.path for f in rec.fields] == ["mail_accounts[address=x@gmail.com].enabled"]
    assert core.settings.mail_accounts[0].app_password == "secretpass"


async def test_writing_a_secret_and_loosening_confirmations_ask(harness: Harness):
    core = harness.core
    for change, word in (
        ({"discord_webhook_url": "https://discord.example/hook"}, "writes a secret"),
        ({"confirmations.mode": "off"}, "loosens what stops for Arsen"),
    ):
        risk = await core.registry.assess("settings.set", {"set": change})
        assert risk is not None and word in risk.reason, change


async def test_concurrent_writes_from_the_ui_and_a_tool_both_land(harness: Harness):
    core = harness.core

    async def tool_side(i: int) -> None:
        await core.admin.apply(core.admin.plan({"voice.hold_volume": round(0.01 * i, 2)}), who=Who("jarvis"))

    async def ui_side(i: int) -> None:
        await core.admin.apply_body({"voice": {"rate": f"+{i}%"}})

    await asyncio.gather(*(f(i) for i in range(1, 11) for f in (tool_side, ui_side)))
    stored = await core.store.load_settings()
    assert stored.voice.hold_volume == core.settings.voice.hold_volume and stored.voice.rate == core.settings.voice.rate
    assert core.settings.voice.rate.startswith("+") and core.settings.voice.hold_volume <= 0.1


async def test_ui_save_over_an_older_revision_is_a_conflict(harness: Harness):
    core = harness.core
    rev = await core.admin.rev()
    await core.admin.apply_body({"voice": {"rate": "+5%"}}, base_rev=rev)
    with pytest.raises(SettingsConflict):
        await core.admin.apply_body({"voice": {"rate": "+9%"}}, base_rev=rev)
    with pytest.raises(SettingsInvalid):
        await core.admin.apply_body({"voice": {"turn_threshold": 3}})
    assert core.settings.voice.rate == "+5%"
    [rec] = await core.admin.changes.list()
    assert rec.actor == "arsen"


async def test_incognito_changes_apply_and_keep_their_trail(harness: Harness):
    core = harness.core
    harness.chat.push(call("settings.set", {"set": {"voice.hold_volume": 0.25}}), FakeTurn(text="ok"))
    conv = await core.store.create_conversation(incognito=True)
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="quieter", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    [rec] = await core.admin.changes.list()
    assert core.settings.voice.hold_volume == 0.25 and rec.actor_label == "incognito chat"
    await core.delete_conversation(conv.id)
    kept = await core.admin.changes.get(rec.id)
    assert kept is not None and kept.conversation_id is None


# --- runs nobody watches, collab, frozen policy, secrets --------------------------------------


async def test_a_scheduled_run_applies_a_harmless_change_and_proposes_a_risky_one(harness: Harness):
    core = harness.core
    _run, seen = await run_chat(
        harness,
        call("settings.set", {"set": {"voice.hold_volume": 0.2}}, "a"),
        call("settings.set", {"set": {"confirmations.mode": "off"}, "reason": "fewer cards"}, "b"),
        kind=RunKind.SCHEDULED,
        conv_kind=ConversationKind.SCHEDULED,
    )
    assert not any(e.type == "tool.confirm_requested" for e in seen)
    assert core.settings.voice.hold_volume == 0.2 and core.settings.confirmations.mode == "destructive"
    _harmless, proposed = tool_results(seen)
    assert "Proposed, NOT applied" in proposed.text
    rec = next(c for c in await core.admin.changes.list() if c.status == "proposed")
    assert rec.reason == "fewer cards" and "safety" in rec.risk
    applied = await core.admin.apply_proposal(rec.id)
    assert applied.id == rec.id and applied.status == "applied" and core.settings.confirmations.mode == "off"


async def test_a_risky_delete_in_a_scheduled_run_is_not_done(harness: Harness):
    core = harness.core
    sched = await core.schedules.create(name="Keep", prompt="x", cron="0 9 * * *", tz="UTC")
    harness.enable(confirmations=Confirmations(outbound_check=[]))
    _run, seen = await run_chat(
        harness, call("schedule.delete", {"id": sched.id}), kind=RunKind.SCHEDULED, conv_kind=ConversationKind.SCHEDULED
    )
    # schedule.delete is destructive by its flag: unattended runs never asked for those and still
    # do not; its risk hook only words the reason. It is the settings and routes that propose.
    assert not any(e.type == "tool.confirm_requested" for e in seen)


async def test_a_run_keeps_the_approval_rules_it_started_with(harness: Harness):
    """It cannot switch confirmations off and use that in the same run."""
    core = harness.core
    sched = await core.schedules.create(name="Old", prompt="x", cron="0 9 * * *", tz="UTC")
    harness.chat.push(
        call("settings.set", {"set": {"confirmations.mode": "off"}}, "a"),
        call("schedule.delete", {"id": sched.id}, "b"),
        FakeTurn(text="ok"),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    first = next(e for e in seen if e.type == "tool.confirm_requested")
    assert first.name == "settings.set"
    await core.engine.confirm(run.id, first.call_id, True)
    seen = await harness.wait_for(sub, "run.waiting_user")
    second = next(e for e in seen if e.type == "tool.confirm_requested")
    assert second.name == "schedule.delete" and core.settings.confirmations.mode == "off"
    await core.engine.confirm(run.id, second.call_id, False)
    await harness.wait_for(sub, "run.done")


async def test_collab_runs_get_no_admin_tools(harness: Harness):
    core = harness.core
    _run, seen = await run_chat(
        harness, call("settings.get", {"path": "voice"}), kind=RunKind.COLLAB, conv_kind=ConversationKind.COLLAB
    )
    offered = {t.name for t in harness.chat.calls[0][1]}
    assert "settings.get" not in offered and "settings.set" not in offered
    [result] = tool_results(seen)
    assert result.kind.value == "error" and "never a collab session" in result.text
    del core


async def test_a_secret_seen_in_clear_asks_before_it_leaves(harness: Harness):
    core = harness.core
    core.apply_settings(core.settings.model_copy(update={"discord_webhook_url": "https://hook.example/xyz123"}))
    harness.chat.push(
        call("settings.get", {"path": "discord_webhook_url", "reveal": True}, "a"),
        call("notes.add", {"board": "Inbox", "text": "hook is https://hook.example/xyz123"}, "b"),
        FakeTurn(text="ok"),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="note the hook", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.waiting_user")
    req = next(e for e in seen if e.type == "tool.confirm_requested")
    assert req.name == "notes.add" and "stored secret" in req.reason
    await core.engine.confirm(run.id, req.call_id, False)
    await harness.wait_for(sub, "run.done")
    assert "xyz123" not in core.admin.redact("the hook is https://hook.example/xyz123")


async def test_a_risk_hook_that_raises_counts_as_risky(harness: Harness):
    class Broken(BuiltinProvider):
        name = "broken"

        @tool("broken.do", description="x")
        async def _do(self) -> ToolResult:
            return ToolResult.data("did")

        async def assess(self, name: str, arguments: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

    core = harness.core
    core.registry.add(Broken())
    await core.registry.refresh()
    risk = await core.registry.assess("broken.do", {})
    assert risk is not None and "could not judge" in risk.reason


# --- schedules ------------------------------------------------------------------------------


async def test_schedule_get_update_fires_and_revert(harness: Harness):
    core = harness.core
    sched = await core.schedules.create(name="Сутрешен брифинг", prompt="Brief me" * 750, cron="0 8 * * *", tz="UTC")
    harness.chat.push(FakeTurn(text="fired"))
    await core.scheduler.run_now(sched)
    _run, seen = await run_chat(
        harness,
        call("schedule.get", {"id": sched.id}, "a"),
        call("schedule.update", {"id": sched.id, "cron": "30 7 * * *", "prompt": "Brief me at 7:30"}, "b"),
        call("schedule.fires", {"id": sched.id}, "c"),
        call("schedule.update", {"id": sched.id, "cron": "not a cron"}, "d"),
        call("schedule.update", {"id": "sch_nope", "enabled": False}, "e"),
    )
    got, updated, fires, bad, unknown = tool_results(seen)
    assert "Сутрешен брифинг" in got.text and "Brief me" * 750 in got.text and "last fires:" in got.text
    after = await core.schedules.get(sched.id)
    assert after is not None and after.cron == "30 7 * * *" and after.prompt == "Brief me at 7:30"
    assert after.prompt_by is None  # written at Arsen's asking in his own chat
    assert len(await core.schedules.fires(sched.id)) == 1 and "UTC" in fires.text
    assert "invalid cron" in bad.text and "nothing was changed" in bad.text
    assert "no schedule" in unknown.text
    rec = next(c for c in await core.admin.changes.list() if c.area == "schedule")
    assert rec.revertible and {f.path for f in rec.fields} == {"cron", "prompt"}
    assert updated.change["id"] == rec.id
    await core.admin.revert(rec.id, who=Who())
    back = await core.schedules.get(sched.id)
    assert back is not None and back.cron == "0 8 * * *" and back.prompt == "Brief me" * 750


async def test_schedule_pause_resume_and_clearing_think(harness: Harness):
    core = harness.core
    sched = await core.schedules.create(
        name="S", prompt="x", cron="0 9 * * *", tz="UTC", think=True, think_level="high"
    )
    paused = await core.schedules.update(sched.id, enabled=False)
    assert paused is not None and paused.next_fire is None
    resumed = await core.schedules.update(sched.id, enabled=True, think=None, think_level=None)
    assert (
        resumed is not None and resumed.next_fire is not None and resumed.think is None and resumed.think_level is None
    )
    with pytest.raises(ValueError, match="in the past"):
        await core.schedules.update(sched.id, at=datetime.now(UTC) - timedelta(hours=2))


async def test_schedule_create_applies_at_once_and_delete_asks_naming_what_is_lost(harness: Harness):
    core = harness.core
    harness.chat.push(
        call("schedule.create", {"name": "Standup", "prompt": "prep", "cron": "30 8 * * 1-5", "call_me": "never"}, "a"),
        FakeTurn(text="ok"),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="standup", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert not any(e.type == "tool.confirm_requested" for e in seen)
    [s] = await core.schedules.list()
    assert s.call_me == "never"
    risk = await core.registry.assess("schedule.delete", {"id": s.id})
    assert risk is not None and "Standup" in risk.reason and "0 fire" in risk.reason


async def test_a_prompt_a_run_wrote_fires_strict_until_accepted(harness: Harness):
    core = harness.core
    sched = await core.schedules.create(name="Fix me", prompt="old", cron="0 9 * * *", tz="UTC")
    _run, seen = await run_chat(
        harness,
        call("schedule.update", {"id": sched.id, "prompt": "new prompt"}),
        kind=RunKind.SCHEDULED,
        conv_kind=ConversationKind.SCHEDULED,
    )
    after = await core.schedules.get(sched.id)
    assert after is not None and after.prompt == "new prompt" and after.prompt_by is not None
    harness.chat.push(FakeTurn(text="x"))
    run_id, conv_id = await core.scheduler.run_now(after)
    run = await core.store.get_run(run_id)
    assert run is not None and await core.admin.strict(run)
    await core.schedules.accept(sched.id)
    assert not await core.admin.strict(run)
    del seen, conv_id


async def test_a_fire_cannot_run_its_own_schedule_again(harness: Harness):
    core = harness.core
    sched = await core.schedules.create(name="Loop", prompt="x", cron="0 9 * * *", tz="UTC")
    harness.chat.push(call("schedule.run_now", {"id": sched.id}), FakeTurn(text="ok"))
    _run_id, conv_id = await core.scheduler.run_now(sched)
    sub = harness.subscribe(conv_id)
    seen = await harness.wait_for(sub, "run.done")
    result = next(e.result for e in seen if e.type == "tool.result")
    assert "cannot fire itself" in result.text


# --- the generic route tools -----------------------------------------------------------------


@pytest.fixture
def api(harness: Harness) -> Harness:
    create_app(harness.core.config, core=harness.core)  # sets core.app; the lifespan is not needed
    asyncio.get_event_loop()
    return harness


async def _reindex(h: Harness) -> None:
    await h.core.registry.refresh()


async def test_api_describe_lists_marked_routes_only(api: Harness):
    await _reindex(api)
    out = await api.core.registry.call("jarvis.api_describe", {}, cancel=asyncio.Event(), idempotency_key="k")
    assert "PATCH /api/boards/{board_id}" in out.text and "DELETE /api/boards/{board_id} [asks first]" in out.text
    assert "/api/stt" not in out.text and "chat/completions" not in out.text and "POST /api/collab/keys" not in out.text
    assert "PATCH /api/settings [use settings.set]" in out.text
    one = await api.core.registry.call(
        "jarvis.api_describe", {"path": "/api/folders"}, cancel=asyncio.Event(), idempotency_key="k2"
    )
    assert "body: name: string (required)" in one.text


async def test_api_read_write_and_refusals(api: Harness):
    core = api.core
    await _reindex(api)
    ev = asyncio.Event()
    board = await core.boards.create_board("Old name")
    read = await core.registry.call("jarvis.api_read", {"path": "/api/boards"}, cancel=ev, idempotency_key="r")
    assert "Old name" in read.text
    wrote = await core.registry.call(
        "jarvis.api_write",
        {"method": "PATCH", "path": f"/api/boards/{board.id}", "body": {"name": "Нов борд"}},
        cancel=ev,
        idempotency_key="w",
    )
    assert wrote.text.startswith("HTTP 200") and [b.name for b in await core.boards.list_boards()] == ["Нов борд"]
    [rec] = await core.admin.changes.list(area="api")
    assert rec.target == f"PATCH /api/boards/{board.id}" and not rec.revertible
    for method, path, word in (
        ("POST", "/api/stt", "not available to Jarvis"),
        ("POST", "/api/collab/keys", "not available to Jarvis"),
        ("POST", "/api/tasks/t_1/allow-tool", "not available to Jarvis"),
        ("PATCH", "/api/settings", "use settings.set"),
        ("POST", "/api/nowhere", "no route"),
    ):
        out = await core.registry.call(
            "jarvis.api_write", {"method": method, "path": path}, cancel=ev, idempotency_key=path
        )
        assert word in out.text, (path, out.text)
    bad = await core.registry.call(
        "jarvis.api_write",
        {"method": "POST", "path": "/api/folders", "body": {"name": " "}},
        cancel=ev,
        idempotency_key="b",
    )
    assert "HTTP 422" in bad.text and await core.store.list_folders() == []
    risk = await core.registry.assess("jarvis.api_write", {"method": "DELETE", "path": f"/api/boards/{board.id}"})
    assert risk is not None and risk.risk == ["delete"]
    assert (
        await core.registry.assess(
            "jarvis.api_write", {"method": "POST", "path": "/api/folders", "body": {"name": "x"}}
        )
        is None
    )
    bulk = await core.registry.assess(
        "jarvis.api_write",
        {"method": "POST", "path": "/api/conversations/bulk", "body": {"ids": ["c"], "action": "delete"}},
    )
    assert bulk is not None


async def test_api_works_with_an_owner_token(tmp_path: Any):
    from tests.conftest import Harness as H

    h = H(tmp_path)
    h.config.token = "secret"
    await h.start()
    try:
        create_app(h.config, core=h.core)
        await h.core.registry.refresh()
        out = await h.core.registry.call(
            "jarvis.api_read", {"path": "/api/folders"}, cancel=asyncio.Event(), idempotency_key="k"
        )
        assert out.text == "[]"
    finally:
        await h.stop()


async def test_a_run_cannot_delete_its_own_conversation(api: Harness):
    core = api.core
    await _reindex(api)
    conv = await core.store.create_conversation()
    harness = api
    harness.chat.push(
        call("jarvis.api_write", {"method": "DELETE", "path": f"/api/conversations/{conv.id}"}), FakeTurn(text="ok")
    )
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="delete this chat", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    assert not any(e.type == "tool.confirm_requested" for e in seen)
    result = next(e.result for e in seen if e.type == "tool.result")
    assert "speaking in" in result.text and await core.store.get_conversation(conv.id) is not None


# --- the REST trail --------------------------------------------------------------------------


async def test_rest_changes_list_revert_and_etag(api: Harness):
    import httpx

    core = api.core
    transport = httpx.ASGITransport(app=core.app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        got = await c.get("/api/settings")
        rev = got.headers["etag"].strip('"')
        ok = await c.patch("/api/settings", json={"voice": {"hold_volume": 0.15}}, headers={"If-Match": f'"{rev}"'})
        assert ok.status_code == 200 and ok.json()["voice"]["hold_volume"] == 0.15
        stale = await c.patch("/api/settings", json={"voice": {"hold_volume": 0.9}}, headers={"If-Match": f'"{rev}"'})
        assert stale.status_code == 409
        changes = (await c.get("/api/settings/changes")).json()
        assert changes[0]["actor"] == "arsen" and changes[0]["fields"][0]["path"] == "voice.hold_volume"
        undo = await c.post(f"/api/settings/changes/{changes[0]['id']}/revert")
        assert undo.status_code == 200 and core.settings.voice.hold_volume == 0.4
        again = await c.post(f"/api/settings/changes/{changes[0]['id']}/revert")
        assert again.status_code == 409
        assert (await c.post("/api/settings/changes/chg_nope/revert")).status_code == 404
        assert (await c.delete("/api/schedules/sch_nope")).status_code == 404


def test_the_ten_admin_tools_stay_small(harness: Harness):
    """EC-10: every tool rides in every prompt. The route catalogue never does (api_describe)."""
    specs = {s.name: s for s in harness.core.registry.specs()}
    create_app(harness.core.config, core=harness.core)
    asyncio.get_event_loop()
    del specs
    from jarvis_core.features.admin import SettingsTools
    from jarvis_core.features.admin_api import ApiTools
    from jarvis_core.features.schedules import ScheduleTools

    all_specs = {
        s.spec.name: s.spec
        for p in (
            SettingsTools(harness.core),
            ApiTools(harness.core),
            ScheduleTools(harness.core.schedules, tz=lambda: "UTC"),
        )
        for s in p._entries.values()
    }
    size = sum(len(all_specs[n].description) + len(json.dumps(all_specs[n].input_schema)) for n in ADMIN_TOOLS)
    assert size <= 7_200, size
    assert all(all_specs[n].owner_only for n in ADMIN_TOOLS if n.split(".")[0] in ("settings", "jarvis"))


async def test_the_flag_off_takes_the_tools_away(harness: Harness):
    core = harness.core
    plan = core.admin.plan({"agent_admin.enabled": False})
    assert "safety" in core.admin.assess_plan(plan)[0]
    await core.admin.apply(plan, who=Who())
    await core.registry.refresh()
    assert core.registry.get("settings.set") is None
    _ = RunStatus


async def test_the_break_glass_script_reverts_in_the_database(harness: Harness):
    """With the core unreachable: the old values go straight back into the settings rows."""
    import sqlite3

    from jarvis_core import admin_revert

    core = harness.core
    rec = await core.admin.apply(core.admin.plan({"voice.hold_volume": 0.05, "roles.chat.temperature": 0.1}), who=Who())
    assert rec is not None
    conn = sqlite3.connect(core.config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert set(admin_revert.revert(conn, rec.id)) == {"voice.hold_volume", "roles.chat.temperature"}
    finally:
        conn.close()
    stored = await core.store.load_settings()
    assert stored.voice.hold_volume == 0.4 and stored.roles["chat"].temperature == 0.7
    assert (await core.admin.changes.get(rec.id)).status == "reverted"  # type: ignore[union-attr]


async def test_a_one_shot_just_gone_is_still_taken_but_an_old_one_is_refused(harness: Harness):
    """CI 2026-11-15: "remind me now" written as a time a minute ago must not be refused on a slow
    machine; an hour ago is a mistake."""
    core = harness.core
    ok = await core.schedules.create(name="Now", prompt="x", at=datetime.now(UTC) - timedelta(minutes=2), tz="UTC")
    assert ok.next_fire is not None
    with pytest.raises(ValueError, match="in the past"):
        await core.schedules.create(name="Old", prompt="x", at=datetime.now(UTC) - timedelta(hours=1), tz="UTC")
