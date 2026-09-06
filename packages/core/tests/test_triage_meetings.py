"""Triage job and meetings service against a fake host provider (no Outlook, no Whisper)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
from pydantic import BaseModel

from jarvis_core.features.stt import SttResult
from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import MeetingRsvpSettings, RunStatus, ToolResult, TriageSettings
from tests.conftest import Harness


class _ListArgs(BaseModel):
    account: str
    folder: str = "Inbox"
    since: str | None = None
    limit: int = 50


class _MoveArgs(BaseModel):
    entry_id: str
    folder: str


class _ReadArgs(BaseModel):
    entry_id: str
    account: str = ""
    max_chars: int = 20_000


class _FolderCreateArgs(BaseModel):
    path: str
    account: str = ""


class _MeetingArgs(BaseModel):
    meeting_id: str
    after_seq: int = 0


class _InvitesArgs(BaseModel):
    account: str = ""
    days: int = 14


class _RespondArgs(BaseModel):
    entry_id: str
    decision: str = "accept"
    comment: str = ""
    account: str = ""


class _SlotsArgs(BaseModel):
    account: str = ""
    start: str | None = None
    days: int = 5
    duration_min: int = 30
    work_start_hour: int = 9
    work_end_hour: int = 18
    limit: int = 3


class _CanceledArgs(BaseModel):
    account: str = ""
    days_back: int = 1
    days_ahead: int = 60


_CLASH = {"subject": "Sprint review", "start": "2026-09-07T11:00:00+03:00", "end": "2026-09-07T12:00:00+03:00"}
_INVITES = [
    {
        "entry_id": "i1",
        "subject": "Free sync",
        "organizer": "Maria",
        "organizer_address": "maria@bank.bg",
        "start": "2026-09-07T10:00:00+03:00",
        "end": "2026-09-07T10:30:00+03:00",
        "conflicts": [],
    },
    {
        "entry_id": "i2",
        "subject": "Clash",
        "organizer": "Pete",
        "organizer_address": "pete@bank.bg",
        "start": "2026-09-07T11:00:00+03:00",
        "end": "2026-09-07T11:30:00+03:00",
        "conflicts": [_CLASH],
    },
    {
        "entry_id": "i3",
        "subject": "Vendor pitch",
        "organizer": "Sales",
        "organizer_address": "sales@vendor.com",
        "start": "2026-09-07T13:00:00+03:00",
        "end": "2026-09-07T14:00:00+03:00",
        "conflicts": [],
    },
    {
        "entry_id": "i4",
        "subject": "Board prep",
        "organizer": "Boss",
        "organizer_address": "boss@bank.bg",
        "start": "2026-09-07T11:00:00+03:00",
        "end": "2026-09-07T12:00:00+03:00",
        "conflicts": [_CLASH],
    },
]


class FakeHost(BuiltinProvider):
    """Stands in for jarvis-host: two inbox items, records moves, serves one audio chunk."""

    name = "laptop"

    def __init__(self) -> None:
        self.moves: list[tuple[str, str]] = []
        # Shaped exactly like jarvis-host's outlook_list rows (from + flat aliases + preview).
        self.items = [
            {
                "entry_id": "e1",
                "subject": "RE: budget approval",
                "from": {"name": "Rumen Petrov", "address": "rumen@bank.bg"},
                "sender": "Rumen Petrov",
                "received": "2026-09-05T08:00:00",
                "preview": "As agreed in DM-4521 the budget is approved.",  # demand only in the body
            },
            {
                "entry_id": "e2",
                "subject": "Team lunch on Friday?",
                "from": {"name": "Maria", "address": "maria@bank.bg"},
                "received": "2026-09-05T08:05:00",
                "preview": "who is in",
            },
            {
                "entry_id": "e3",
                "subject": "Invoice 2026-118",
                "from": {"name": "", "address": "billing@vendor.com"},
                "received": "2026-09-05T08:10:00",
                "preview": "payment due",
            },
            {
                # A digest: the (short) preview shows ONE demand, the body names three. Must not be
                # filed under DM-1096 - the body is read before a body-only demand match counts.
                "entry_id": "e4",
                "subject": "Jira Email Summary - 04.09.2026",
                "from": {"name": "Arsen", "address": "arsen@bank.bg"},
                "received": "2026-09-05T08:15:00",
                "preview": "Today: DM-1096 extraction moved to PROD ...",
            },
        ]
        self.bodies = {
            "e1": "As agreed in DM-4521 the budget is approved. Regards, Rumen",
            "e4": "Today: DM-1096 extraction moved to PROD. DM-1945 ECAT waits UAT. DM-2167 estimation due.",
        }
        self.reads: list[str] = []
        self.folders_created: list[str] = []
        self.pulled = 0
        self.invites = [dict(i) for i in _INVITES]
        self.responses: list[tuple[str, str, str]] = []
        self.canceled_calls = 0
        super().__init__()

    @tool("laptop.calendar_invites", description="invites", args=_InvitesArgs, read_only=True)
    async def _invites(self, account: str = "", days: int = 14) -> ToolResult:
        return ToolResult.data(json.dumps({"invites": self.invites}))

    @tool("laptop.calendar_respond", description="respond", args=_RespondArgs)
    async def _respond(self, entry_id: str, decision: str = "accept", comment: str = "", account: str = "") -> ToolResult:
        self.responses.append((entry_id, decision, comment))
        return ToolResult.data(json.dumps({"sent": True, "decision": decision}))

    @tool("laptop.calendar_free_slots", description="slots", args=_SlotsArgs, read_only=True)
    async def _slots(self, **_: Any) -> ToolResult:
        # 09:30 is consecutive to 09:00: a spread proposal skips it in favour of 14:00.
        slots = [
            {"start": "2026-09-08T09:00:00+03:00", "end": "2026-09-08T09:30:00+03:00"},
            {"start": "2026-09-08T09:30:00+03:00", "end": "2026-09-08T10:00:00+03:00"},
            {"start": "2026-09-08T14:00:00+03:00", "end": "2026-09-08T14:30:00+03:00"},
            {"start": "2026-09-09T09:00:00+03:00", "end": "2026-09-09T09:30:00+03:00"},
        ]
        return ToolResult.data(json.dumps({"slots": slots}))

    @tool("laptop.calendar_remove_canceled", description="canceled", args=_CanceledArgs)
    async def _canceled(self, account: str = "", days_back: int = 1, days_ahead: int = 60) -> ToolResult:
        self.canceled_calls += 1
        return ToolResult.data(json.dumps({"removed": 1}))

    @tool("laptop.outlook_accounts", description="accounts", read_only=True)
    async def _accounts(self) -> ToolResult:
        return ToolResult.data(json.dumps({"accounts": [{"name": "Work"}]}))

    @tool("laptop.outlook_list", description="list", args=_ListArgs, read_only=True)
    async def _list(self, account: str, folder: str = "Inbox", since: str | None = None, limit: int = 50) -> ToolResult:
        items = [i for i in self.items if since is None or i["received"] > since]
        return ToolResult.data(json.dumps({"items": items, "cursor": None}))

    @tool("laptop.outlook_move", description="move", args=_MoveArgs)
    async def _move(self, entry_id: str, folder: str) -> ToolResult:
        self.moves.append((entry_id, folder))
        return ToolResult.data("moved")

    @tool("laptop.outlook_folder_create", description="mkdir", args=_FolderCreateArgs)
    async def _folder_create(self, path: str, account: str = "") -> ToolResult:
        self.folders_created.append(path)
        return ToolResult.data(json.dumps({"path": path, "created": [path]}))

    @tool("laptop.outlook_read", description="read", args=_ReadArgs, read_only=True)
    async def _read(self, entry_id: str, account: str = "", max_chars: int = 20_000) -> ToolResult:
        self.reads.append(entry_id)
        return ToolResult.data(json.dumps({"entry_id": entry_id, "body": self.bodies.get(entry_id, "")[:max_chars]}))

    @tool("laptop.meeting_start", description="start", args=_MeetingArgs)
    async def _mstart(self, meeting_id: str, after_seq: int = 0) -> ToolResult:
        return ToolResult.data("recording")

    @tool("laptop.meeting_stop", description="stop", args=_MeetingArgs)
    async def _mstop(self, meeting_id: str, after_seq: int = 0) -> ToolResult:
        return ToolResult.data("stopped")

    @tool("laptop.meeting_pull", description="pull", args=_MeetingArgs, read_only=True)
    async def _mpull(self, meeting_id: str, after_seq: int = 0) -> ToolResult:
        if self.pulled:
            return ToolResult.empty()
        self.pulled += 1
        wav = base64.b64encode(b"RIFF....fake").decode()
        return ToolResult.data(json.dumps({"chunks": [{"seq": 1, "t0": 0.0, "t1": 30.0, "wav_base64": wav}]}))


async def _with_host(harness: Harness) -> FakeHost:
    host = FakeHost()
    harness.core.registry.add(host)
    await harness.core.registry.refresh()
    return host


async def test_triage_routes_demands_structurally_and_classifies_the_rest(harness: Harness):
    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            enabled=True,
            host="laptop",
            accounts=["Work"],
            categories=[
                {"name": "invoices", "folder": "Finance/Invoices", "rule": "supplier invoices and payment requests"}
            ],
        )
    )
    # Classifier answers for the three non-demand mails: lunch → none, invoice → invoices, digest → none.
    harness.chat.push(
        FakeTurn(text='{"category": "none"}'),
        FakeTurn(text='{"category": "invoices"}'),
        FakeTurn(text='{"category": "none"}'),
    )
    global_sub = harness.subscribe("none")
    report = await core.triage.run_once()
    assert report.errors == [] and report.processed == 4 and report.routed == 2
    assert host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    # Body-only demand mentions were verified against the real body (e1 → one demand, e4 → three).
    assert host.reads == ["e1", "e4"]
    # Persisted decisions: a second run re-does nothing and moves nothing.
    report2 = await core.triage.run_once()
    assert report2.processed == 0 and host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    states = await core.triage.states()
    assert states[0].account == "Work" and states[0].processed_today == 4 and states[0].cursor == "2026-09-05T08:15:00"
    # The day's triage conversation got one compact message.
    convs = [c for c in await core.store.list_conversations() if c.kind.value == "triage"]
    assert len(convs) == 1 and convs[0].folder_key == "Work"
    msgs = await core.store.list_messages(convs[0].id)
    # The summary names the sender: `from` dict for e1, address-only fallback for e3.
    assert msgs[-1].name == "triage" and "Rumen Petrov" in msgs[-1].content and "Demands/DM-4521" in msgs[-1].content
    assert "billing@vendor.com" in msgs[-1].content
    assert any(getattr(e, "type", "") == "conversation.updated" for e in _drain(global_sub))


async def test_per_account_rules_override_the_defaults_and_folders_are_created_once(harness: Harness):
    """Work mailbox and personal Gmail: different categories, different rules, demand routing off
    for the personal one - and the category folders are created before the first move."""
    from jarvis_proto import TriageRules

    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            categories=[{"name": "invoices", "folder": "Finance/Invoices", "rule": "invoices"}],
            instructions="Owner: Arsen at the bank.",
            account_rules={
                "work": TriageRules(
                    categories=[{"name": "spam", "folder": "Trash/Delete", "rule": "marketing"}],
                    instructions="Personal mailbox rules.",
                    fallback_category="spam",
                    demand_routing=False,
                )
            },
        )
    )
    harness.chat.push(*[FakeTurn(text='{"category": "none"}')] * 4)
    report = await core.triage.run_once()
    assert report.errors == []
    # Every mail lands in the personal catch-all: the override replaced the work categories,
    # and the DM-4521 in the body was NOT routed as a demand (demand_routing off).
    assert host.moves == [(f"e{i}", "Trash/Delete") for i in (1, 2, 3, 4)]
    assert host.folders_created == ["Trash/Delete"]  # once, before the first move; no Demands root
    prompt = harness.chat.calls[-1][0][-1].content
    assert "Personal mailbox rules." in prompt and "Owner: Arsen at the bank." not in prompt
    # A second pass creates nothing again.
    await core.triage.run_once()
    assert host.folders_created == ["Trash/Delete"]


async def test_triage_without_host_reports_instead_of_crashing(harness: Harness):
    harness.enable(triage=TriageSettings(enabled=True, host=""))
    report = await harness.core.triage.run_once()
    assert report.errors and "triage.host" in report.errors[0]


def test_demand_routing_subject_wins_and_digests_are_not_filed():
    from jarvis_core.features.triage import demand_folder

    kw = {"prefixes": ["DM-"], "root": "Demands"}
    assert demand_folder("RE: DM-2186 budget", "mentions DM-1096 and DM-1945 too", **kw) == "Demands/DM-2186"
    assert demand_folder("re: dm-2186 budget", "", **kw) == "Demands/DM-2186"  # case-insensitive, normalised
    assert demand_folder("RE: budget approval", "As agreed in DM-4521 the budget is approved.", **kw) == "Demands/DM-4521"
    # A digest: several distinct demands in the body, none in the subject -> nowhere.
    assert demand_folder("Jira Email Summary - 04.09.2026", "DM-1945 ECAT ... DM-1096 extraction ... DM-2167", **kw) is None
    # The same demand repeated is still one demand.
    assert demand_folder("FW: status", "DM-1945 is late. DM-1945 owner asked.", **kw) == "Demands/DM-1945"
    # Guards against look-alikes.
    assert demand_folder("ADM-1234 rollout", "foo1234 DM-12345678", **kw) is None


async def test_triage_dry_run_proposes_but_moves_records_and_advances_nothing(harness: Harness):
    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            instructions="Owner: Arsen. Cc-only mail is reference.",
            fallback_category="reference",
            categories=[
                {"name": "invoices", "folder": "Finance/Invoices", "rule": "supplier invoices and payment requests"},
                {"name": "reference", "folder": "Action Hub/Reference", "rule": "everything else"},
            ],
        )
    )
    harness.chat.push(
        FakeTurn(text='{"category": "none"}'),
        FakeTurn(text='{"category": "invoices"}'),
        FakeTurn(text='{"category": "none"}'),
    )
    report = await core.triage.run_once(dry_run=True)
    assert report.dry_run and report.errors == [] and report.processed == 4 and report.routed == 4
    assert host.moves == []  # nothing moved
    # "none" lands in the configured catch-all (inbox zero), the rest where the classifier said;
    # the digest is NOT a demand although its preview names one.
    assert [(p["subject"], p["folder"]) for p in report.proposed] == [
        ("RE: budget approval", "Demands/DM-4521"),
        ("Team lunch on Friday?", "Action Hub/Reference"),
        ("Invoice 2026-118", "Finance/Invoices"),
        ("Jira Email Summary - 04.09.2026", "Action Hub/Reference"),
    ]
    assert all(p["current_folder"] == "Inbox" for p in report.proposed)
    # Nothing persisted: no state row, no decisions, no triage conversation.
    assert await core.triage.states() == []
    assert await core.db.fetchone("SELECT 1 FROM triage_decisions") is None
    assert [c for c in await core.store.list_conversations() if c.kind.value == "triage"] == []
    # The classifier saw the owner rules, the sender's ADDRESS (VIP lists are addresses) and the
    # recipient lines.
    prompts = [c[0][-1].content for c in harness.chat.calls]
    assert all("Owner: Arsen" in p and "To: " in p and "Cc: " in p for p in prompts[-3:])
    assert any("<billing@vendor.com>" in p for p in prompts)

    # A folder sample is a dry run of an already-sorted folder: proposal vs where the mail lives.
    harness.chat.push(*[FakeTurn(text='{"category": "invoices"}')] * 3)
    sample = await core.triage.run_once(dry_run=True, folder="Finance/Invoices", limit=10)
    assert sample.processed == 4 and all(p["current_folder"] == "Finance/Invoices" for p in sample.proposed)
    assert host.moves == []
    # ...and never a live run.
    live = await core.triage.run_once(folder="Finance/Invoices")
    assert live.errors and "dry run" in live.errors[0] and host.moves == []


async def test_rsvp_accepts_free_declines_clashes_never_declines_vip_and_answers_once(harness: Harness):
    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        rsvp=MeetingRsvpSettings(
            host="laptop", account="Work", allowed_domains=["bank.bg"], vip=["boss@bank.bg"], propose_slots=2
        )
    )
    # Dry run: every decision visible, nothing sent, nothing recorded, cancelled meetings untouched.
    dry = await core.rsvp.run_once(dry_run=True)
    assert dry.dry_run and dry.errors == [] and dry.pending == 4
    assert host.responses == [] and host.canceled_calls == 0
    assert [(d.subject, d.decision) for d in dry.decisions] == [
        ("Free sync", "accept"),
        ("Clash", "decline"),
        ("Vendor pitch", "left_external"),
        ("Board prep", "accept_vip_conflict"),
    ]
    assert dry.decisions[1].proposals == ["вт 08.09 09:00-09:30", "вт 08.09 14:00-14:30"]
    assert dry.decisions[1].conflicts == ["Sprint review 11:00-12:00"]
    assert await core.rsvp.state() is None and await core.db.fetchone("SELECT 1 FROM rsvp_decisions") is None

    # Live: accept, decline with the proposals in the comment, leave the external one, accept the VIP.
    live = await core.rsvp.run_once()
    assert live.errors == [] and live.removed_canceled == 1 and host.canceled_calls == 1
    assert [(r[0], r[1]) for r in host.responses] == [("i1", "accept"), ("i2", "decline"), ("i4", "accept")]
    decline_comment = host.responses[1][2]
    assert "09:00-09:30" in decline_comment and "14:00-14:30" in decline_comment and "Jarvis" in decline_comment
    state = await core.rsvp.state()
    assert state and state.answered_total == 3 and state.removed_canceled_total == 1 and state.last_error is None

    # Second pass: the ledger answers nothing twice although the host still lists the same invites.
    again = await core.rsvp.run_once()
    assert again.decisions == [] and len(host.responses) == 3
    recent = await core.rsvp.decisions()
    assert sorted(r["decision"] for r in recent) == ["accept", "accept_vip_conflict", "decline", "left_external"]

    # The day's RSVP conversation carries one line per decision.
    convs = [c for c in await core.store.list_conversations() if c.folder_label == "Calendar RSVP"]
    assert len(convs) == 1
    msgs = await core.store.list_messages(convs[0].id)
    assert msgs[-1].name == "rsvp" and "'Clash'" in msgs[-1].content and "left_external" in msgs[-1].content


async def test_rsvp_without_host_or_domains_fails_closed(harness: Harness):
    await _with_host(harness)
    harness.enable(rsvp=MeetingRsvpSettings(host=""))
    rep = await harness.core.rsvp.run_once()
    assert rep.errors and "rsvp.host" in rep.errors[0]
    harness.enable(rsvp=MeetingRsvpSettings(host="laptop", account="Work"))  # no allowed_domains, no vip
    rep = await harness.core.rsvp.run_once(dry_run=True)
    assert any("allowed_domains" in e for e in rep.errors)
    assert {d.decision for d in rep.decisions} == {"left_external"}


async def test_meeting_records_transcribes_and_summarises(harness: Harness, monkeypatch: Any):
    core = harness.core
    await _with_host(harness)

    async def fake_transcribe(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        return SttResult(
            text="Welcome everyone. Budget is approved.",
            language="en",
            backend="fake",
            duration_ms=5,
            segments=[
                {"t0": 0.0, "t1": 2.0, "text": "Welcome everyone."},
                {"t0": 2.0, "t1": 5.0, "text": "Budget is approved."},
            ],
        )

    monkeypatch.setattr(core.transcriber, "transcribe", fake_transcribe)
    harness.chat.push(FakeTurn(text="Summary: budget approved; action: Arsen to inform the team."))
    meeting = await core.meetings.start(title="Steering", host="laptop")
    sub = harness.subscribe(meeting.conversation_id)
    assert meeting.status.value == "recording"
    # Pull once by hand (the poller ticks every 10 s) and check the transcript landed.
    await core.meetings._pull_once(meeting)
    detail = await core.meetings.detail(meeting.id)
    assert detail is not None and [s.text for s in detail.segments] == ["Welcome everyone.", "Budget is approved."]
    segs = [e for e in _drain(sub) if getattr(e, "type", "") == "meeting.segment"]
    assert len(segs) == 2 and segs[1].t0 == 2.0
    msgs = await core.store.list_messages(meeting.conversation_id)
    assert msgs[-1].name == "transcript" and "[00:02] Budget is approved." in msgs[-1].content

    stopped = await core.meetings.stop(meeting.id)
    assert stopped.status.value == "summarising" and stopped.summary_run_id
    async with asyncio.timeout(10):
        while (await core.meetings.get(meeting.id)).status.value == "summarising":  # type: ignore[union-attr]
            await asyncio.sleep(0.05)
    final = await core.meetings.get(meeting.id)
    assert final is not None and final.status.value == "done"
    run = await core.store.get_run(stopped.summary_run_id)  # type: ignore[arg-type]
    assert run is not None and run.status is RunStatus.DONE and run.kind.value == "meeting"
    # The summary run saw the transcript message as context.
    assert any(m.name == "transcript" for m in harness.chat.calls[-1][0])
    msgs = await core.store.list_messages(meeting.conversation_id)
    assert "budget approved" in msgs[-1].content.lower()

    # Frames: stored on disk, served by path, OCR text becomes context.
    seq = await core.meetings.add_frame(meeting.id, b"\x89PNG fake", 12.5, ocr="Slide 3: Q3 numbers")
    path = await core.meetings.frame_path(meeting.id, seq)
    assert path is not None and path.exists()


async def test_meeting_start_fails_loudly_when_host_cannot_capture(harness: Harness):
    class DeadHost(BuiltinProvider):
        name = "dead"

        @tool("dead.meeting_start", description="start", args=_MeetingArgs)
        async def _s(self, meeting_id: str, after_seq: int = 0) -> ToolResult:
            return ToolResult.failure("no audio device")

    harness.core.registry.add(DeadHost())
    await harness.core.registry.refresh()
    try:
        await harness.core.meetings.start(title="x", host="dead")
    except RuntimeError as exc:
        assert "no audio device" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert (await harness.core.meetings.list())[0].status.value == "failed"


def _drain(sub: Any) -> list[Any]:
    out: list[Any] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def test_httpx_available_for_stt_mock():
    assert httpx.__version__
