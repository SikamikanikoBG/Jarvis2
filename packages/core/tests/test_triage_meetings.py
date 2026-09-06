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


class _NotifyArgs(BaseModel):
    text: str


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
                "received": "2026-09-05T08:15:00",
                "preview": "As agreed in DM-4521 the budget is approved.",  # demand only in the body
            },
            {
                "entry_id": "e2",
                "subject": "Team lunch on Friday?",
                "from": {"name": "Maria", "address": "maria@bank.bg"},
                "received": "2026-09-05T08:10:00",
                "preview": "who is in",
            },
            {
                "entry_id": "e3",
                "subject": "Invoice 2026-118",
                "from": {"name": "", "address": "billing@vendor.com"},
                "received": "2026-09-05T08:05:00",
                "preview": "payment due",
            },
            {
                # A digest: the (short) preview shows ONE demand, the body names three. Must not be
                # filed under DM-1096 - the body is read before a body-only demand match counts.
                "entry_id": "e4",
                "subject": "Jira Email Summary - 04.09.2026",
                "from": {"name": "Arsen", "address": "arsen@bank.bg"},
                "received": "2026-09-05T08:00:00",
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
        self.warning = ""  # what meeting_start reports about the devices
        self.chunk_seq: int | None = None  # set to 0 to number chunks like the real host
        self.after_seqs: list[int] = []  # what the core told the host to skip past
        self.invites = [dict(i) for i in _INVITES]
        self.responses: list[tuple[str, str, str]] = []
        self.canceled_calls = 0
        # Entry ids the host refuses to act on, as a real Outlook does when COM hiccups. Clear
        # the set to let the next attempt through.
        self.move_fails: set[str] = set()
        self.respond_fails: set[str] = set()
        self.since_seen: list[str | None] = []  # what triage asked the host to filter by
        super().__init__()

    @tool("laptop.calendar_invites", description="invites", args=_InvitesArgs, read_only=True)
    async def _invites(self, account: str = "", days: int = 14) -> ToolResult:
        return ToolResult.data(json.dumps({"invites": self.invites}))

    @tool("laptop.calendar_respond", description="respond", args=_RespondArgs)
    async def _respond(self, entry_id: str, decision: str = "accept", comment: str = "", account: str = "") -> ToolResult:
        if entry_id in self.respond_fails:
            return ToolResult.failure("The messaging interface has returned an unknown error")
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
        self.since_seen.append(since)
        items = [i for i in self.items if since is None or i["received"] > since]
        # Newest first and capped, exactly like the host's GetTable read.
        items = sorted(items, key=lambda i: str(i["received"]), reverse=True)[:limit]
        return ToolResult.data(json.dumps({"items": items, "cursor": None}))

    @tool("laptop.outlook_move", description="move", args=_MoveArgs)
    async def _move(self, entry_id: str, folder: str) -> ToolResult:
        if entry_id in self.move_fails:
            return ToolResult.failure("cannot move: the item is open in another window")
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
        return ToolResult.data(json.dumps({"recording": True, "levels": {"mic": 800}, "warning": self.warning}))

    @tool("laptop.meeting_stop", description="stop", args=_MeetingArgs)
    async def _mstop(self, meeting_id: str, after_seq: int = 0) -> ToolResult:
        if self.chunk_seq is None:
            return ToolResult.data("stopped")
        self.chunk_seq += 1  # the real host returns its last chunk WITH the stop
        return ToolResult.data(json.dumps({"chunks": [self._chunk()], "recording": False}))

    def _chunk(self) -> dict[str, object]:
        wav = base64.b64encode(b"RIFF....fake").decode()
        t0 = (self.chunk_seq - 1) * 10.0
        return {"seq": self.chunk_seq, "t0": t0, "t1": t0 + 10.0, "wav_base64": wav}

    @tool("laptop.meeting_pull", description="pull", args=_MeetingArgs, read_only=True)
    async def _mpull(self, meeting_id: str, after_seq: int = 0) -> ToolResult:
        self.after_seqs.append(after_seq)
        if self.chunk_seq is not None:
            # Like the real host: a chunk whose number is not past `after_seq` is withheld.
            self.chunk_seq += 1
            chunk = self._chunk()
            chunks = [chunk] if chunk["seq"] > after_seq else []  # type: ignore[operator]
            return ToolResult.data(json.dumps({"chunks": chunks}))
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


async def test_vip_alerts_are_structural_and_push_once_per_pass(harness: Harness):
    """Ported from V1's alerts_config.json: a named sender (or a subject keyword) buzzes Arsen's
    phone the moment it arrives, whatever the classifier thinks of the mail."""
    from jarvis_proto import TriageAlert

    core = harness.core
    host = await _with_host(harness)
    pushed: list[str] = []

    class FakeNotify(BuiltinProvider):
        name = "notify"

        @tool("notify.discord", description="push", args=_NotifyArgs)
        async def _discord(self, text: str) -> ToolResult:
            pushed.append(text)
            return ToolResult.data("sent")

    core.registry.add(FakeNotify())
    await core.registry.refresh()
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            categories=[{"name": "reference", "folder": "Action Hub/Reference", "rule": "everything"}],
            fallback_category="reference",
            alerts=[
                TriageAlert(name="Petya Dimitrova", senders=["rumen@bank.bg"]),
                TriageAlert(name="Invoices", keywords=["invoice"]),
                TriageAlert(name="Off", enabled=False, senders=["maria@bank.bg"]),
                TriageAlert(name="Empty"),  # no senders and no keywords: matches nothing
            ],
        )
    )
    harness.chat.push(*[FakeTurn(text='{"category": "reference"}')] * 4)
    report = await core.triage.run_once()
    assert report.errors == []
    # The VIP (by address) and the keyword mail; Maria's alert is off, and "Empty" matches nothing.
    assert [(a["alert"], a["sender"]) for a in report.alerts] == [
        ("Petya Dimitrova", "Rumen Petrov"),
        ("Invoices", "billing@vendor.com"),
    ]
    # One push for the pass, listing both, with where each was filed.
    assert len(pushed) == 1
    assert "2 mail(s)" in pushed[0] and "[Petya Dimitrova] Rumen Petrov" in pushed[0] and "[Invoices]" in pushed[0]
    # The day's triage message marks them too.
    convs = [c for c in await core.store.list_conversations() if c.kind.value == "triage"]
    text = (await core.store.list_messages(convs[0].id))[-1].content
    assert "[ALERT Petya Dimitrova]" in text and "[ALERT Invoices]" in text
    # Mail still goes where the rules say; an alert changes nothing about filing.
    assert ("e2", "Action Hub/Reference") in host.moves

    # A dry run reports the alerts and sends nothing. (Sampling the folder, because the live pass
    # above moved the cursor past these mails - which is exactly what it should have done.)
    pushed.clear()
    harness.chat.push(*[FakeTurn(text='{"category": "reference"}')] * 4)
    dry = await core.triage.run_once(dry_run=True, folder="Inbox")
    assert pushed == [] and [a["alert"] for a in dry.alerts] == ["Petya Dimitrova", "Invoices"]
    assert [p["alert"] for p in dry.proposed if p["alert"]] == ["Petya Dimitrova", "Invoices"]


def test_auto_replies_from_a_vip_do_not_buzz_the_phone():
    """Measured on the real mailbox: one of four VIP alerts was 'Automatic reply: ...'."""
    from jarvis_proto import TriageAlert
    from jarvis_proto.settings import is_auto_reply

    assert is_auto_reply("Automatic reply: Proposal for the CEO")
    assert is_auto_reply("RE: Отн: Автоматичен отговор: отпуска")
    assert is_auto_reply("Accepted: Weekly sync") and is_auto_reply("Undeliverable: report")
    # A real mail that merely mentions it is not one.
    assert not is_auto_reply("Please set an out of office before Friday")
    assert not is_auto_reply("RE: Protocol signing")

    vip = TriageAlert(name="Rumen", senders=["rradushev@postbank.bg"])
    assert vip.matches("rradushev@postbank.bg", "Rumen Radushev", "RE: Protocol signing")
    assert not vip.matches("rradushev@postbank.bg", "Rumen Radushev", "Automatic reply: I am away")
    loud = TriageAlert(name="Rumen", senders=["rradushev@postbank.bg"], skip_auto_replies=False)
    assert loud.matches("rradushev@postbank.bg", "Rumen Radushev", "Automatic reply: I am away")


async def test_triage_retries_a_mail_it_could_not_move_and_says_it_could_not(harness: Harness):
    """A move that failed leaves the mail in the Inbox — so it must stay triageable.

    The decision row is written BEFORE the move (right, for crash safety) and used to be left
    behind claiming a move that never happened, so the mail was invisible to every later pass —
    while report.errors was empty, so the API answered "0 errors" about a mailbox it had quietly
    given up on. What makes the retry work is that the row is dropped and the mail is still in
    the Inbox, which is the queue; the received time it carries is not part of it.
    """
    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            categories=[{"name": "invoices", "folder": "Finance/Invoices", "rule": "invoices"}],
        )
    )
    host.move_fails = {"e3"}  # the invoice cannot be filed this time
    # e1 is a demand (no classifier); the other three are lunch → none, invoice → invoices,
    # digest → none.
    harness.chat.push(
        FakeTurn(text='{"category": "none"}'),
        FakeTurn(text='{"category": "invoices"}'),
        FakeTurn(text='{"category": "none"}'),
    )

    first = await core.triage.run_once()
    assert host.moves == [("e1", "Demands/DM-4521")]  # the demand went, the invoice did not
    assert first.routed == 1
    assert any("Invoice 2026-118" in e and "not moved to Finance/Invoices" in e for e in first.errors)
    # Nothing claims the move happened, and the failure is on the account's status.
    assert await core.db.fetchone("SELECT 1 FROM triage_decisions WHERE entry_id = 'e3'") is None
    states = await core.triage.states()
    assert states[0].last_error and "Invoice" in states[0].last_error
    # The day's summary tells Arsen too, rather than only the log.
    convs = [c for c in await core.store.list_conversations() if c.kind.value == "triage"]
    assert "move failed" in (await core.store.list_messages(convs[0].id))[-1].content

    # Next pass: only the stuck mail is re-classified, and this time it lands.
    host.move_fails = set()
    harness.chat.push(FakeTurn(text='{"category": "invoices"}'))
    second = await core.triage.run_once()
    assert second.errors == [] and second.routed == 1 and second.processed == 1
    assert host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    states = await core.triage.states()
    assert states[0].cursor == "2026-09-05T08:15:00" and states[0].last_error is None

    # ...and a third pass has nothing left to do.
    third = await core.triage.run_once()
    assert third.processed == 0 and third.routed == 0 and len(host.moves) == 2


async def test_mail_left_in_the_inbox_is_never_stranded_behind_a_clock(harness: Harness):
    """The Inbox is the queue. A mail is skipped because it was DECIDED, not because of when it
    arrived.

    Filtering the listing by a received-time watermark let the watermark outrun mail that was
    never sorted, with no way back. Measured on the real postbank mailbox 2026-09-06: four mails
    sat in the Inbox while the cursor stood two hours past them, and every pass reported
    "0 processed, no errors" — the classifier was fine, it was simply never shown them.
    """
    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            demand_routing=False,
            categories=[{"name": "reference", "folder": "Action Hub/Reference", "rule": "anything informational"}],
        )
    )
    # One mail arrives and is filed, pushing any watermark to 09:00.
    host.items = [{"entry_id": "new", "subject": "Latest", "from": {"name": "A"}, "received": "2026-09-06T09:00:00"}]
    harness.chat.push(FakeTurn(text='{"category": "reference"}'))
    first = await core.triage.run_once()
    assert first.processed == 1 and host.moves == [("new", "Action Hub/Reference")]

    # Now an OLDER mail turns up in the Inbox — a delayed delivery, or one Arsen moved back.
    # Under a watermark it is invisible for ever; it must simply be the next thing in the queue.
    host.items = [{"entry_id": "old", "subject": "Arrived late", "from": {"name": "B"}, "received": "2026-09-06T07:30:00"}]
    harness.chat.push(FakeTurn(text='{"category": "reference"}'))
    second = await core.triage.run_once()
    assert second.processed == 1, "a mail older than the cursor was never looked at"
    assert host.moves[-1] == ("old", "Action Hub/Reference")
    assert host.since_seen and all(s is None for s in host.since_seen), f"still filtering by time: {host.since_seen}"

    # And it is still decided exactly once: the Inbox no longer holds it, and a re-list of what
    # remains does not re-do the work.
    host.items = []
    third = await core.triage.run_once()
    assert third.processed == 0 and len(host.moves) == 2


async def test_a_backlog_bigger_than_one_page_drains_instead_of_being_skipped(harness: Harness):
    """More mail than one listing holds must be worked through, not jumped over.

    The host returns the NEWEST `limit` above the watermark and the watermark then moved to the
    newest of those, so everything between was stranded — which is what a night of downtime on a
    work mailbox looks like.
    """
    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            demand_routing=False,
            categories=[{"name": "reference", "folder": "Action Hub/Reference", "rule": "anything"}],
        )
    )
    host.items = [
        {"entry_id": f"m{i:02d}", "subject": f"Mail {i}", "from": {"name": "S"}, "received": f"2026-09-06T{7 + i // 10:02d}:{i % 10:02d}:00"}
        for i in range(12)
    ]
    harness.chat.push(*[FakeTurn(text='{"category": "reference"}')] * 12)

    # Five at a time: three passes must file all twelve, oldest included.
    for _ in range(3):
        report = await core.triage.run_once(limit=5)
        for moved, _folder in host.moves:
            host.items = [i for i in host.items if i["entry_id"] != moved]
        assert report.errors == []
    assert len(host.moves) == 12, f"only {len(host.moves)} of 12 were ever filed"
    assert {m[0] for m in host.moves} == {f"m{i:02d}" for i in range(12)}


async def test_triage_counts_what_this_pass_routed_across_every_account(harness: Harness):
    """report.routed is this pass's moves. It used to be assigned `state.routed_today` inside
    the per-account loop, so the last account overwrote the others (two accounts routing 3 and
    0 reported 0) and a second pass on one account reported the whole day's total."""
    from jarvis_proto import TriageRules

    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work", "Personal"],
            demand_routing=False,
            categories=[{"name": "invoices", "folder": "Finance/Invoices", "rule": "invoices"}],
            account_rules={"personal": TriageRules(categories=[], demand_routing=False)},  # files nothing
        )
    )
    harness.chat.push(*[FakeTurn(text='{"category": "invoices"}')] * 4)  # Work: all four filed
    report = await core.triage.run_once()
    assert sorted(report.accounts) == ["Personal", "Work"]
    assert report.processed == 8 and report.routed == 4  # not 0, which is Personal's day total
    assert len(host.moves) == 4

    # A second pass over the same day adds nothing: the count is per pass, not per day.
    again = await core.triage.run_once()
    assert again.processed == 0 and again.routed == 0
    states = {s.account: s for s in await core.triage.states()}
    assert states["Work"].routed_today == 4 and states["Personal"].routed_today == 0


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


async def test_rsvp_retries_an_invite_it_could_not_answer(harness: Harness):
    """A failed calendar_respond must not enter the ledger.

    The ledger is what stops the next pass from trying again, so recording a "failed" row
    retired the invite for good after one COM hiccup — and report.errors was empty, so nothing
    said so. Arsen finds out by missing the meeting.
    """
    core = harness.core
    host = await _with_host(harness)
    host.invites = [dict(_INVITES[0])]  # one clean accept, nothing else in the way
    host.respond_fails = {"i1"}
    harness.enable(rsvp=MeetingRsvpSettings(host="laptop", account="Work", allowed_domains=["bank.bg"]))

    first = await core.rsvp.run_once()
    assert [d.decision for d in first.decisions] == ["failed"]
    assert host.responses == []
    assert any("Free sync" in e and "unknown error" in e for e in first.errors)
    # Nothing in the ledger, and the run's own status says why.
    assert await core.db.fetchone("SELECT 1 FROM rsvp_decisions") is None
    state = await core.rsvp.state()
    assert state is not None and state.answered_total == 0 and state.last_error and "Free sync" in state.last_error

    # The next pass answers it, because the invite was never marked as decided.
    host.respond_fails = set()
    second = await core.rsvp.run_once()
    assert [d.decision for d in second.decisions] == ["accept"]
    assert [(r[0], r[1]) for r in host.responses] == [("i1", "accept")]
    state = await core.rsvp.state()
    assert state is not None and state.answered_total == 1 and state.last_error is None

    # ...and only once: now it IS in the ledger.
    third = await core.rsvp.run_once()
    assert third.decisions == [] and len(host.responses) == 1


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
    # Pull once by hand (the poller ticks every 10 s) and check the transcript landed. Pulling and
    # transcribing are separate now, so wait for the transcription worker to catch up.
    await core.meetings._pull_once(meeting)
    await core.meetings.settle(meeting)
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


async def test_a_silent_meeting_says_so_instead_of_summarising_something_else(harness: Harness, monkeypatch: Any):
    """The first real silent recording came back as a summary of an unrelated Teams call: with no
    transcript, "summarise the transcript above" makes the model reach for whatever it remembers."""
    core = harness.core
    await _with_host(harness)

    async def silent(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        return SttResult(text="", language="en", backend="fake", duration_ms=5, segments=[])

    monkeypatch.setattr(core.transcriber, "transcribe", silent)
    host = next(p for p in core.registry._providers if getattr(p, "name", "") == "laptop")  # type: ignore[attr-defined]
    host.warning = "the microphone delivered digital silence (muted, or access is off)"
    meeting = await core.meetings.start(title="Quiet room", host="laptop")
    # The dead device is announced at the start, not discovered at the end.
    started_msgs = await core.store.list_messages(meeting.conversation_id)
    assert started_msgs[-1].name == "meeting" and "digital silence" in started_msgs[-1].content
    await core.meetings._pull_once(meeting)
    await core.meetings.settle(meeting)
    stopped = await core.meetings.stop(meeting.id)

    assert stopped.status.value == "done" and stopped.summary_run_id is None
    assert await core.store.list_runs(meeting.conversation_id) == []  # no model call at all
    assert harness.chat.calls == []
    msgs = await core.store.list_messages(meeting.conversation_id)
    assert msgs[-1].name == "meeting" and "nothing to summarise" in msgs[-1].content
    assert "microphone" in msgs[-1].content  # and it says what to check
    assert "What the host measured" in msgs[-1].content


async def test_no_chunk_is_lost_between_the_pulls_or_at_the_stop(harness: Harness, monkeypatch: Any):
    """A real 68-second family recording came back with 22 seconds missing in the middle and the
    last 26 gone. Three causes, all here: the core sent the host a SEGMENT count where it expects
    a CHUNK number (so the host discarded chunks whose number had fallen behind), the closing
    chunk that meeting_stop returns was never read, and transcription blocked the next pull."""
    core = harness.core
    host = await _with_host(harness)

    async def transcribe(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        await asyncio.sleep(0.05)  # Whisper is slow; the pulls must not wait for it
        return SttResult(
            text="one two three",
            language="en",
            backend="fake",
            duration_ms=5,
            # Three segments per chunk: this is what pushed the segment count past the chunk number.
            segments=[{"t0": 0.0, "t1": 1.0, "text": "one"}, {"t0": 1.0, "t1": 2.0, "text": "two"}, {"t0": 2.0, "t1": 3.0, "text": "three"}],
        )

    monkeypatch.setattr(core.transcriber, "transcribe", transcribe)
    host.chunk_seq = 0  # the fake host numbers its chunks like the real one
    meeting = await core.meetings.start(title="Family", host="laptop")

    for _ in range(4):
        await core.meetings._pull_once(meeting)
    await core.meetings.settle(meeting)
    assert host.after_seqs == [0, 1, 2, 3], f"the host was told {host.after_seqs}, not chunk numbers"
    detail = await core.meetings.detail(meeting.id)
    assert detail is not None and len(detail.segments) == 12  # four chunks, nothing discarded

    stopped = await core.meetings.stop(meeting.id)
    detail = await core.meetings.detail(meeting.id)
    assert detail is not None and len(detail.segments) == 15  # the closing chunk landed too
    assert stopped.status.value == "summarising"


async def test_shutdown_leaves_no_meeting_task_running(harness: Harness, monkeypatch: Any):
    """Core.stop closes the database; nothing this service owns may still be writing to it.

    stop_all cancelled the pollers only. A transcriber outlives its poller by design — that is
    the whole point of the split — so one was always left running, mid-ingest_chunk, against a
    connection being closed under it.
    """
    core = harness.core
    await _with_host(harness)

    async def slow(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        await asyncio.sleep(30)  # still transcribing when the process goes down
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(core.transcriber, "transcribe", slow)
    meeting = await core.meetings.start(title="Long one", host="laptop")
    await core.meetings._pull_once(meeting)
    await asyncio.sleep(0)  # let the transcriber pick the chunk up
    tasks = [core.meetings._pollers[meeting.id], core.meetings._transcribers[meeting.id]]
    assert [t.done() for t in tasks] == [False, False]

    await core.meetings.stop_all()
    assert all(t.done() for t in tasks), "a meeting task survived shutdown"
    assert core.meetings._pollers == {} and core.meetings._transcribers == {}


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
