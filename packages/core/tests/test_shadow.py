"""Shadow decisions: recorded next to production, never obeyed, never in the way.

What must hold, one test each: production does exactly what it did before (same moves, same
answers) whether the shadow is on, off, or its model is down; every decision point writes one
row with the input production saw and the answer it gave; nothing from an incognito chat reaches
the shadow file; a burst past ``max_pending`` is dropped and counted, not queued.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from jarvis_core.models.fake import FakeTurn
from jarvis_proto import INCOGNITO_TITLE, MeetingRsvpSettings, ShadowSettings, ToolCall, TriageAlert, TriageSettings
from tests.conftest import Harness
from tests.test_triage_meetings import _with_host


def _fake_laya(seen: list[dict[str, Any]], *, fail: bool = False) -> httpx.AsyncClient:
    """A laya-service that answers every question with its first option (or a clean 0.9)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if fail:
            raise httpx.ConnectError("laya is down")
        answers: dict[str, Any] = {}
        for qid, q in body["questions"].items():
            if q["type"] == "choice":
                first = next(iter(q["criteria"]))
                answers[qid] = {"type": "choice", "choice": first, "probabilities": {first: 0.9}}
            else:
                answers[qid] = {"type": "noul", "noul": 0.9}
        return httpx.Response(200, json={"answers": answers, "ms": 31.5, "checkpoint": "multilingual"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _shadow_on(harness: Harness, seen: list[dict[str, Any]], *, fail: bool = False, **extra: Any) -> None:
    harness.core.shadow._client = _fake_laya(seen, fail=fail)
    harness.enable(shadow=ShadowSettings(enabled=True, laya_url="http://laya:9140", **extra))


def _triage(**extra: Any) -> TriageSettings:
    return TriageSettings(
        enabled=True,
        host="laptop",
        accounts=["Work"],
        instructions="Cc-only mail is reference.",
        categories=[{"name": "invoices", "folder": "Finance/Invoices", "rule": "supplier invoices"}],
        alerts=[TriageAlert(name="vendors", senders=["@vendor.com"])],
        **extra,
    )


async def _rows(harness: Harness, point: str | None = None) -> list[dict[str, Any]]:
    await harness.core.shadow.drain()
    rows = await harness.core.shadow.rows()
    return [r for r in rows if point is None or r["point"] == point]


async def test_triage_records_every_mail_next_to_what_production_did(harness: Harness):
    core = harness.core
    host = await _with_host(harness)
    seen: list[dict[str, Any]] = []
    _shadow_on(harness, seen)
    harness.enable(triage=_triage())
    harness.chat.push(
        FakeTurn(text='{"category": "none"}'),
        FakeTurn(text='{"category": "invoices"}'),
        FakeTurn(text='{"category": "none"}'),
    )
    report = await core.triage.run_once()
    # Production unchanged: the same two moves the shadow-less test asserts.
    assert host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    assert report.alerts and report.alerts[0]["alert"] == "vendors"

    rows = await _rows(harness, "mail")
    assert [r["ref"] for r in rows] and sorted(r["ref"] for r in rows) == ["e1", "e2", "e3", "e4"]
    by_ref = {r["ref"]: r for r in rows}
    # Who decided: the DM-regex needs no model, and then there is no model latency to compare.
    assert by_ref["e1"]["prod"]["category_by"] == "regex" and by_ref["e1"]["prod_ms"] is None
    assert by_ref["e3"]["prod"] == {
        "category": "invoices",
        "folder": "Finance/Invoices",
        "category_by": "model",
        "alert": "vendors",
        "alert_by": "rules",
    }
    assert by_ref["e3"]["prod_ms"] is not None and by_ref["e3"]["source"] == "live"
    # Laya answered, and its answer is stored as it came.
    assert by_ref["e3"]["laya"]["answers"]["category"]["choice"] == "invoices" and by_ref["e3"]["laya_ms"] == 31.5
    # What Laya was sent: the mail first, the owner's rules last, nothing private-underscored;
    # both questions in one request.
    sent = next(b for b in seen if b["state"]["subject"] == "Invoice 2026-118")
    assert list(sent["state"])[-2:] == ["filing_rules", "alert_rules"]
    assert not any(k.startswith("_") for k in sent["state"])
    assert set(sent["questions"]) == {"category", "alert"}
    assert "none" in sent["questions"]["category"]["criteria"]
    # ...while the record keeps everything, the category list included, for the replay.
    assert by_ref["e3"]["input"]["_categories"][0]["name"] == "invoices"


async def test_production_is_identical_when_laya_is_down(harness: Harness):
    core = harness.core
    host = await _with_host(harness)
    _shadow_on(harness, [], fail=True)
    harness.enable(triage=_triage())
    harness.chat.push(
        FakeTurn(text='{"category": "none"}'),
        FakeTurn(text='{"category": "invoices"}'),
        FakeTurn(text='{"category": "none"}'),
    )
    report = await core.triage.run_once()
    # The only error is the test's own: its alert has no webhook to go to. Nothing about Laya.
    assert all(e.startswith("alert not delivered") for e in report.errors)
    assert host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    rows = await _rows(harness, "mail")
    # Still recorded - production's half is the useful half for a later replay.
    assert len(rows) == 4 and all(r["laya"] is None and "laya is down" in r["laya_error"] for r in rows)


async def test_nothing_is_recorded_when_the_shadow_is_off(harness: Harness):
    core = harness.core
    await _with_host(harness)
    harness.enable(triage=_triage())  # shadow left at its default: off
    harness.chat.push(
        FakeTurn(text='{"category": "none"}'),
        FakeTurn(text='{"category": "invoices"}'),
        FakeTurn(text='{"category": "none"}'),
    )
    await core.triage.run_once()
    assert await _rows(harness) == []


async def test_a_folder_sample_is_recorded_with_its_folder_as_ground_truth(harness: Harness):
    core = harness.core
    await _with_host(harness)
    _shadow_on(harness, [])
    harness.enable(triage=_triage())
    harness.chat.push(*[FakeTurn(text='{"category": "none"}') for _ in range(3)])
    await core.triage.run_once(dry_run=True, folder="Finance/Invoices", limit=10)
    rows = await _rows(harness, "mail")
    assert rows and all(r["source"] == "folder_sample" for r in rows)
    assert all(r["meta"]["current_folder"] == "Finance/Invoices" for r in rows)


async def test_rsvp_records_every_policy_decision_and_the_decline_text(harness: Harness):
    core = harness.core
    host = await _with_host(harness)
    _shadow_on(harness, [])
    harness.enable(
        rsvp=MeetingRsvpSettings(enabled=True, host="laptop", allowed_domains=["bank.bg"], vip=["boss@bank.bg"])
    )
    await core.rsvp.run_once()
    assert len(host.responses) == 3  # unchanged: free → accept, clash → decline, VIP → accept

    rows = {r["input"]["subject"]: r for r in await _rows(harness, "rsvp")}
    assert {s: r["prod"]["decision"] for s, r in rows.items()} == {
        "Free sync": "accept",
        "Clash": "decline",
        "Vendor pitch": "left_external",  # recorded too, though nothing was sent
        "Board prep": "accept_vip_conflict",
    }
    assert "boss@bank.bg" in rows["Board prep"]["input"]["policy"]
    assert rows["Clash"]["input"]["conflicts"] and rows["Free sync"]["input"]["conflicts"] == ["none"]
    # The decline comment is outgoing text on Arsen's behalf: the guard's question is asked of it.
    guard = await _rows(harness, "guardrail")
    assert len(guard) == 1 and guard[0]["source"] == "rsvp" and "Здравейте" in guard[0]["input"]["text"]
    assert guard[0]["prod"] == {"sent": True, "by": "none"}


async def test_preflight_is_recorded_for_a_chat_and_never_for_an_incognito_one(harness: Harness):
    core = harness.core
    _shadow_on(harness, [])
    harness.enable(planning_enabled=True)
    ask = "Please check the homelab numbers for this week and tell me which box used most power."

    harness.chat.push(FakeTurn(text='{"tier": "simple"}'), FakeTurn(text="vader, by far."))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text=ask, conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")

    harness.chat.push(FakeTurn(text='{"tier": "simple"}'), FakeTurn(text="vader again."))
    private = await core.store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    sub2 = harness.subscribe(private.id)
    await core.engine.create_run(text=ask, conversation_id=private.id)
    await harness.wait_for(sub2, "run.done")

    rows = await _rows(harness, "preflight")
    assert [r["ref"] for r in rows] == [run.id]
    assert rows[0]["prod"]["tier"] == "simple" and rows[0]["prod"]["tier_by"] == "model"
    assert rows[0]["input"] == {"request": ask} and rows[0]["source"] == "chat"


async def test_outgoing_text_from_a_chat_is_guarded_but_not_from_an_incognito_one(harness: Harness):
    core = harness.core
    await _with_host(harness)
    _shadow_on(harness, [], guardrail_tools=["*.calendar_respond"])
    say = {"entry_id": "i1", "decision": "decline", "comment": "Sorry, busy - Jarvis"}
    for incognito in (False, True):
        harness.chat.push(
            FakeTurn(tool_calls=[ToolCall(id=f"c{incognito}", name="laptop.calendar_respond", arguments=dict(say))]),
            FakeTurn(text="Declined."),
        )
        conv = await core.store.create_conversation(title=INCOGNITO_TITLE if incognito else "work", incognito=incognito)
        sub = harness.subscribe(conv.id)
        await core.engine.create_run(text="decline the sync", conversation_id=conv.id)
        await harness.wait_for(sub, "run.done")
    rows = await _rows(harness, "guardrail")
    assert len(rows) == 1 and rows[0]["input"]["text"] == "Sorry, busy - Jarvis"
    assert rows[0]["source"] == "chat" and rows[0]["meta"] == {"tool": "laptop.calendar_respond"}


async def test_a_burst_past_max_pending_is_dropped_and_counted(harness: Harness):
    core = harness.core
    _shadow_on(harness, [], max_pending=2)
    for i in range(5):
        core.shadow.observe("mail", ref=f"m{i}", input={"subject": "x"}, questions={}, prod={})
    assert core.shadow.dropped == 3
    assert len(await _rows(harness, "mail")) == 2


def test_guardrail_globs():
    s = ShadowSettings(enabled=True, guardrail_tools=["*.outlook_send", "notify.discord"])
    assert s.guards("workocholic.outlook_send") and s.guards("notify.discord")
    assert not s.guards("workocholic.outlook_list") and not s.guards("notify.discordx")
    assert not ShadowSettings().observes("mail")  # off by default
