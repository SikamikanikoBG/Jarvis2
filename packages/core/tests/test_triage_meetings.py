"""Triage job and meetings service against a fake host provider (no Outlook, no Whisper)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
from pydantic import BaseModel

from jarvis_core.features.stt import SttResult
from jarvis_core.features.triage import TriageReport, backoff_interval
from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import MeetingRsvpSettings, RunStatus, ToolResult, TriageSettings
from tests.conftest import Harness


class _ListArgs(BaseModel):
    account: str
    folder: str = "Inbox"
    since: str | None = None
    limit: int = 50
    cursor: str | None = None


class _MoveArgs(BaseModel):
    entry_id: str
    folder: str


class _ReadArgs(BaseModel):
    entry_id: str
    account: str = ""
    max_chars: int = 20_000


class _ThreadArgs(BaseModel):
    entry_id: str
    account: str = ""
    limit: int = 8
    preview_chars: int = 600


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
        # conversation_id -> the conversation outlook_thread returns (oldest first)
        self.threads: dict[str, list[dict]] = {}
        self.thread_calls: list[str] = []
        self.folders_created: list[str] = []
        self.pulled = 0
        self.warning = ""  # what meeting_start reports about the devices
        self.chunk_seq: int | None = None  # set to 0 to number chunks like the real host
        self.after_seqs: list[int] = []  # what the core told the host to skip past
        self.invites = [dict(i) for i in _INVITES]
        self.responses: list[tuple[str, str, str]] = []
        self.canceled_calls = 0
        self.slots_override: list[dict] | None = None
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
    async def _respond(
        self, entry_id: str, decision: str = "accept", comment: str = "", account: str = ""
    ) -> ToolResult:
        if entry_id in self.respond_fails:
            return ToolResult.failure("The messaging interface has returned an unknown error")
        self.responses.append((entry_id, decision, comment))
        return ToolResult.data(json.dumps({"sent": True, "decision": decision}))

    @tool("laptop.calendar_free_slots", description="slots", args=_SlotsArgs, read_only=True)
    async def _slots(self, **_: Any) -> ToolResult:
        # 09:30 is consecutive to 09:00: a spread proposal skips it in favour of 14:00.
        if self.slots_override is not None:
            return ToolResult.data(json.dumps({"slots": self.slots_override}))
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
    async def _list(
        self, account: str, folder: str = "Inbox", since: str | None = None, limit: int = 50, cursor: str | None = None
    ) -> ToolResult:
        self.since_seen.append(since)
        items = [i for i in self.items if since is None or i["received"] > since]
        # Newest first and capped, exactly like the host's GetTable read; the cursor is an offset.
        items = sorted(items, key=lambda i: str(i["received"]), reverse=True)
        start = int(cursor or 0)
        nxt = str(start + limit) if start + limit < len(items) else None
        return ToolResult.data(json.dumps({"items": items[start : start + limit], "cursor": nxt}))

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

    @tool("laptop.outlook_thread", description="thread", args=_ThreadArgs, read_only=True)
    async def _thread(self, entry_id: str, account: str = "", limit: int = 8, preview_chars: int = 600) -> ToolResult:
        self.thread_calls.append(entry_id)
        conv = next((str(i.get("conversation_id") or "") for i in self.items if i["entry_id"] == entry_id), "")
        if conv not in self.threads:
            return ToolResult.data(json.dumps({"conversation": False, "items": []}))
        return ToolResult.data(json.dumps({"conversation": True, "items": self.threads[conv][-limit:]}))

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


async def test_a_sender_gated_category_takes_only_its_senders_and_only_none_falls_back(harness: Harness):
    """Measured on the real mailbox: Bosses filled with mail where a boss was only in Cc, and
    Reference swallowed every answer the classifier fumbled. A boss writing is filed as a boss
    without asking the model; nobody else can be put there; only an explicit 'none' is caught."""
    from jarvis_proto import TriageRules

    core = harness.core
    host = await _with_host(harness)
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            account_rules={
                "work": TriageRules(
                    categories=[
                        {
                            "name": "bosses",
                            "folder": "Leadership/Bosses",
                            "rule": "my bosses",
                            "senders": "maria@bank.bg",
                        },
                        {"name": "reference", "folder": "Action Hub/Reference", "rule": "FYI"},
                    ],
                    fallback_category="reference",
                    demand_routing=False,
                )
            },
        )
    )
    # e2 (Maria, the boss) never reaches the model: three turns for e1, e3, e4.
    harness.chat.push(
        FakeTurn(text='{"category": "bosses"}'),  # e1 Rumen: not a boss, so not on offer
        FakeTurn(text="I think this is an invoice"),  # e3: garbled
        FakeTurn(text='{"category": "none"}'),  # e4: an honest none
    )
    report = await core.triage.run_once()
    assert report.errors == []
    assert host.moves == [("e2", "Leadership/Bosses"), ("e4", "Action Hub/Reference")]
    assert len(harness.chat.calls) == 3
    prompt = harness.chat.calls[0][0][-1].content
    assert "- reference:" in prompt and "- bosses:" not in prompt

    # A meeting response from a boss is the mail system talking: it goes to the model. Addresses
    # match case-insensitively, a bare domain admits the whole domain, no address admits nobody.
    rules = TriageRules(categories=[{"name": "b", "folder": "B", "senders": "Maria@Bank.bg; @board.bg"}])
    assert rules.sender_category("maria@bank.bg", "Accepted: Weekly sync") is None
    assert rules.sender_category("ceo@board.bg", "RE: budget")["folder"] == "B"
    assert not rules.admits(rules.categories[0], "rumen@bank.bg") and not rules.admits(rules.categories[0], "")


async def test_a_pass_pages_past_mail_left_in_the_inbox(harness: Harness):
    """Measured 2026-09-29: ~50 mails left in the Inbox on purpose sat on top, and the pass only
    ever read the newest 50 - older, never-sorted mail trickled through and then stopped."""
    core = harness.core
    host = await _with_host(harness)
    host.items = [
        {"entry_id": f"m{n:03d}", "subject": f"mail {n}", "from": {"name": "X", "address": "x@bank.bg"},
         "received": f"2026-09-{1 + n // 24:02d}T{n % 24:02d}:00:00", "preview": ""}
        for n in range(120)
    ]  # fmt: skip
    harness.enable(triage=TriageSettings(host="laptop", accounts=["Work"]))
    newest_first = sorted(host.items, key=lambda i: i["received"], reverse=True)
    for item in newest_first[:60]:  # decided earlier and left in the Inbox
        await core.triage._record(item["entry_id"], "Work", None, "left")
    report = await core.triage.run_once()
    assert report.errors == [] and report.processed == 50
    # The 50 processed are the next 50 after the 60 left ones, not a re-read of the top.
    decided = {i["entry_id"] for i in newest_first[60:110]}
    assert all([await core.triage._decided(e, "Work") for e in decided])
    assert not await core.triage._decided(newest_first[110]["entry_id"], "Work")
    # Next pass: the last 10; then nothing, and the walk ends at the end of the folder.
    assert (await core.triage.run_once()).processed == 10
    assert (await core.triage.run_once()).processed == 0


async def test_the_audit_flags_gate_violations_and_reports_the_judges_disagreements(harness: Harness):
    """The daily audit reads the live decisions from the shadow log: a boss's mail filed
    elsewhere and a non-boss in Bosses are rule violations whatever any model thinks; the judge
    re-checks a sample and every disagreement is listed with its reason."""
    from datetime import UTC, datetime

    from jarvis_proto import TriageRules

    core = harness.core
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            account_rules={
                "work": TriageRules(
                    categories=[
                        {"name": "bosses", "folder": "Leadership/Bosses", "rule": "boss", "senders": "maria@bank.bg"},
                        {"name": "reference", "folder": "Action Hub/Reference", "rule": "FYI"},
                    ],
                    fallback_category="reference",
                )
            },
        )
    )
    now = datetime.now(UTC).isoformat()

    def row(n: int, sender: str, category: str, folder: str, source: str = "live") -> dict:
        return {
            "id": n, "point": "mail", "source": source, "at": now,
            "input": {"from": sender, "to": "", "cc": "", "subject": f"mail {n}", "preview": "", "_account": "Work"},
            "prod": {"category": category, "folder": folder},
        }  # fmt: skip

    rows = [
        row(1, "Maria <maria@bank.bg>", "reference", "Action Hub/Reference"),  # a boss filed elsewhere
        row(2, "Rumen <rumen@bank.bg>", "bosses", "Leadership/Bosses"),  # a non-boss in Bosses
        row(3, "X <x@bank.bg>", "reference", "Action Hub/Reference"),
        row(4, "Y <y@bank.bg>", "reference", "Action Hub/Reference", source="dry_run"),  # not live: ignored
        row(5, "Maria <maria@bank.bg>", "bosses", "Leadership/Bosses"),  # the gate's own call: not judged
    ]

    async def fake_rows(*, since_id: int = 0, limit: int = 1000) -> list[dict]:
        return [r for r in rows if r["id"] > since_id][:limit]

    core.shadow.rows = fake_rows  # type: ignore[method-assign]
    harness.judge.push(
        FakeTurn(text='{"verdict": "ok", "better": "", "why": ""}'),
        FakeTurn(text='{"verdict": "wrong", "better": "none", "why": "asks nothing"}'),
        FakeTurn(text="not json at all"),
    )
    report = await core.triage_audit.run(sample=3, post=True)
    a = report["accounts"]["Work"]
    assert report["decisions"] == 4 and a["decisions"] == 4
    assert a["folders"] == {"Action Hub/Reference": 2, "Leadership/Bosses": 2}
    assert a["catch_all"] == "Action Hub/Reference" and a["catch_all_share"] == 0.5
    assert len(a["violations"]) == 2
    assert any("does not admit: rumen@bank.bg" in v for v in a["violations"])
    assert any("from maria@bank.bg but filed as reference" in v for v in a["violations"])
    # Three judged (rows 1-3); row 5 was filed by the sender gate and is not the judge's call.
    assert a["judged"] == 2 and a["agreement"] == 0.5 and a["judge_errors"] == 1
    assert len(harness.judge.calls) == 3
    assert [d["why"] for d in a["disagreements"]] == ["asks nothing"]
    # Posted to the day's Triage audit conversation.
    convs = [c for c in await core.store.list_conversations() if c.folder_key == "triage-audit"]
    assert len(convs) == 1
    text = (await core.store.list_messages(convs[0].id))[-1].content
    assert "2 rule violation(s)" in text and "judge agrees 50% of 2 sampled" in text


async def test_a_conversation_is_judged_as_a_whole_and_its_inbox_mails_move_together(harness: Harness):
    """Arsen, 2026-09-30: triage a thread, not mail by mail. A reply alone says little; the
    conversation (earlier messages from any folder, his own replies marked) decides, once, and
    every Inbox mail of that thread follows. Structural rules still decide per mail first."""
    from jarvis_proto import TriageRules

    core = harness.core
    host = await _with_host(harness)
    host.items = [
        {"entry_id": "t2", "conversation_id": "C1", "subject": "RE: Branch plan", "received": "2026-09-30T10:00:00",
         "from": {"name": "Pm", "address": "pm@bank.bg"}, "preview": "Agreed."},
        {"entry_id": "t1", "conversation_id": "C1", "subject": "RE: Branch plan", "received": "2026-09-30T09:00:00",
         "from": {"name": "Ops", "address": "ops@bank.bg"}, "preview": "Fine by me."},
        {"entry_id": "b1", "conversation_id": "C1", "subject": "RE: Branch plan", "received": "2026-09-30T08:30:00",
         "from": {"name": "Maria", "address": "maria@bank.bg"}, "preview": "Go ahead."},
        {"entry_id": "s1", "subject": "Lunch?", "received": "2026-09-30T07:00:00",
         "from": {"name": "X", "address": "x@bank.bg"}, "preview": "who is in"},
    ]  # fmt: skip
    host.threads["C1"] = [
        {"entry_id": "o1", "subject": "Branch plan", "received": "2026-09-29T09:00:00",
         "from": {"name": "Pm", "address": "pm@bank.bg"}, "preview": "Arsen, can your team own the rollout?", "mine": False},
        {"entry_id": "o2", "subject": "RE: Branch plan", "received": "2026-09-29T11:00:00",
         "from": {"name": "Arsen", "address": "aapostolov@bank.bg"}, "preview": "We can, draft by Friday.", "mine": True},
        *[{**i, "mine": False} for i in host.items[:3][::-1]],
    ]  # fmt: skip
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            account_rules={
                "work": TriageRules(
                    categories=[
                        {"name": "bosses", "folder": "Leadership/Bosses", "rule": "boss", "senders": "maria@bank.bg"},
                        {"name": "to_read", "folder": "Action Hub/To-Read", "rule": "a thread he takes part in"},
                        {"name": "reference", "folder": "Action Hub/Reference", "rule": "FYI"},
                    ],
                    fallback_category="reference",
                    demand_routing=False,
                )
            },
        )
    )
    harness.chat.push(FakeTurn(text='{"category": "to_read"}'), FakeTurn(text='{"category": "none"}'))
    report = await core.triage.run_once()
    assert report.errors == [] and report.processed == 4
    moves = dict(host.moves)
    # The boss's own message is filed by the gate, per mail; the rest of the thread moves together.
    assert moves == {
        "b1": "Leadership/Bosses",
        "t2": "Action Hub/To-Read",
        "t1": "Action Hub/To-Read",
        "s1": "Action Hub/Reference",
    }
    # One model call per conversation (and one for the lone mail), the thread fetched for the newest.
    assert len(harness.chat.calls) == 2 and host.thread_calls == ["t2"]
    prompt = harness.chat.calls[0][0][-1].content
    assert "Judge the CONVERSATION as a whole" in prompt
    assert "Arsen [OWNER]: RE: Branch plan | We can, draft by Friday." in prompt
    assert "can your team own the rollout?" in prompt
    assert "From: Pm <pm@bank.bg>" in prompt  # the email judged is the newest of the thread
    lone = harness.chat.calls[1][0][-1].content
    assert "conversation" not in lone  # a mail without a thread is judged alone


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
    host.items = [
        {"entry_id": "old", "subject": "Arrived late", "from": {"name": "B"}, "received": "2026-09-06T07:30:00"}
    ]
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
        {
            "entry_id": f"m{i:02d}",
            "subject": f"Mail {i}",
            "from": {"name": "S"},
            "received": f"2026-09-06T{7 + i // 10:02d}:{i % 10:02d}:00",
        }
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
    assert (
        demand_folder("RE: budget approval", "As agreed in DM-4521 the budget is approved.", **kw) == "Demands/DM-4521"
    )
    # A digest: several distinct demands in the body, none in the subject -> nowhere.
    assert (
        demand_folder("Jira Email Summary - 04.09.2026", "DM-1945 ECAT ... DM-1096 extraction ... DM-2167", **kw)
        is None
    )
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
    assert "09:00-09:30" in decline_comment and "14:00-14:30" in decline_comment
    # The sign-off names no assistant: this text goes to the organizer (2026-09-21).
    assert decline_comment.endswith("(автоматичен отговор според календара)")
    assert "Jarvis" not in decline_comment and "Джарвис" not in decline_comment
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


async def test_the_decline_sign_off_is_a_setting_and_can_be_switched_off(harness: Harness):
    """What the organizer reads is Arsen's to write.

    The sign-off used to be a line of Python that named the assistant, and it had already gone
    out to an organizer at the bank before he saw one (2026-09-21). It is a setting now: his own
    wording, or nothing at all.
    """
    core = harness.core
    host = await _with_host(harness)
    base = dict(host="laptop", account="Work", allowed_domains=["bank.bg"], vip=["boss@bank.bg"], propose_slots=1)
    harness.enable(rsvp=MeetingRsvpSettings(**base, decline_signature="Regards,\nArsen\n(sent by my calendar)"))
    await core.rsvp.run_once()
    comment = next(c for eid, d, c in host.responses if d == "decline")
    assert comment.endswith("Regards\nArsen\n(sent by my calendar)".replace("Regards", "Regards,"))
    assert "друг ангажимент" in comment, "the body itself is unchanged"

    # Empty: the note ends with the proposals, with no dangling blank lines.
    harness2 = harness
    core2 = harness2.core
    host.responses.clear()
    await core2.db.execute("DELETE FROM rsvp_decisions")
    await core2.db.execute("DELETE FROM rsvp_state")
    harness2.enable(rsvp=MeetingRsvpSettings(**base, decline_signature=""))
    await core2.rsvp.run_once()
    bare = next(c for eid, d, c in host.responses if d == "decline")
    assert bare.endswith("моля преместете срещата там.")


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
            segments=[
                {"t0": 0.0, "t1": 1.0, "text": "one"},
                {"t0": 1.0, "t1": 2.0, "text": "two"},
                {"t0": 2.0, "t1": 3.0, "text": "three"},
            ],
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


def test_a_pass_that_could_list_no_account_is_blind():
    # 2026-09-27/28 on JarvisVM: Outlook sat on an Office sign-in for hours; every 5-minute pass still
    # fired folder_create + list at it, keeping the COM worker permanently busy on calls that could not work.
    both = ["a@x.bg", "b@x.bg"]
    down = TriageReport(
        accounts=both, errors=[f"{a}: list failed: Error: outlook.list_items did not finish" for a in both]
    )
    assert down.blind
    half = TriageReport(accounts=both, errors=["a@x.bg: list failed: Error: timeout"])
    assert not half.blind
    assert not TriageReport(accounts=both).blind
    assert not TriageReport().blind  # nothing configured is not "down"


def test_blind_passes_back_off_to_half_an_hour_and_recover_at_once():
    base = 5 * 60
    assert [backoff_interval(base, n) for n in range(6)] == [300, 600, 1200, 1800, 1800, 1800]
    assert backoff_interval(45 * 60, 3) == 45 * 60  # a slow base interval is never shortened


def test_previews_lose_the_banner_the_invite_boilerplate_and_the_signature():
    """2026-10-04 Reference sweep: the external-mail banner filled a whole 300-char preview, so
    the classifier judged an external reply on the warning alone."""
    from jarvis_core.features.triage import clean_preview

    banner = (
        "ВНИМАНИЕ: Това е ВЪНШЕН имейл. Работете с повишено внимание – линкове, файлове или искания за "
        "действия може да са опит за фишинг или измама. WARNING: This is an EXTERNAL email. Proceed with "
        "caution – links, attachments, or requests for action may be attempts at phishing or fraud. "
    )
    assert clean_preview(banner + "Арсен, можеш ли да потвърдиш до петък? Поздрави, Иван Петров") == (
        "Арсен, можеш ли да потвърдиш до петък?"
    )
    invite = "Да прегледаме плана. ____________________ Microsoft Teams meeting Join: https://teams.x/1"
    assert clean_preview(invite) == "Да прегледаме плана. [Teams meeting]"
    # A greeting that opens the mail is not mistaken for the signature.
    assert clean_preview("Поздрави, колеги, срокът е утре.") == "Поздрави, колеги, срокът е утре."


async def test_a_thread_is_rejudged_as_it_stands_now_even_with_arsen_in_cc(harness: Harness):
    """Arsen, 2026-10-04: a thread can need nothing for several passes and then need him, Cc or
    not. Each new message re-judges the whole conversation, who it is addressed to included."""
    from jarvis_proto import TriageRules

    core = harness.core
    host = await _with_host(harness)
    host.items = [
        {"entry_id": "n1", "conversation_id": "C9", "subject": "RE: Audit findings", "received": "2026-10-04T10:00:00",
         "from": {"name": "Auditor", "address": "audit@bank.bg"}, "to": "Ops Team", "cc": "Arsen P. Apostolov",
         "preview": "Арсен, екипът ти е отговорен за т. 3 - срок 10.10. Моля потвърди."},
    ]  # fmt: skip
    host.threads["C9"] = [
        {"entry_id": "e1", "subject": "Audit findings", "received": "2026-09-28T09:00:00",
         "from": {"name": "Auditor", "address": "audit@bank.bg"}, "to": "Ops Team", "cc": "Arsen P. Apostolov",
         "preview": "ВНИМАНИЕ: Това е ВЪНШЕН имейл. Бла измама. Report attached for information.", "mine": False},
        {**host.items[0], "mine": False},
    ]  # fmt: skip
    harness.enable(
        triage=TriageSettings(
            host="laptop",
            accounts=["Work"],
            account_rules={
                "work": TriageRules(
                    categories=[
                        {"name": "todo", "folder": "Action Hub/To-Do", "rule": "he must act"},
                        {"name": "reference", "folder": "Action Hub/Reference", "rule": "FYI"},
                    ],
                    fallback_category="reference",
                    demand_routing=False,
                )
            },
        )
    )
    harness.chat.push(FakeTurn(text='{"category": "todo"}'))
    report = await core.triage.run_once()
    assert report.errors == [] and dict(host.moves) == {"n1": "Action Hub/To-Do"}
    prompt = harness.chat.calls[0][0][-1].content
    assert "as it stands NOW" in prompt and "even when he is only in Cc" in prompt
    assert "Auditor (to Ops Team cc Arsen P. Apostolov): Audit findings | Report attached for information." in prompt
    assert "ВЪНШЕН" not in prompt


async def test_a_decline_never_proposes_a_slot_a_later_accept_in_the_same_pass_takes(harness: Harness):
    """A decline for an EARLIER invite must not offer a slot a LATER invite in the same pass
    gets accepted into.

    Invites are decided in start order, and an accept writes the appointment into the
    calendar immediately - but the earlier decline already asked the host for free slots,
    which did not know about the accept. On 2026-10-08 that is how an organizer was
    offered 09.10 14:00-15:00, the very hour 'Centralize administration of ad-hoc
    requests' was accepted into by the same pass. The pass now pre-seeds every slot it
    will commit, so a decline's proposal skips them.
    """
    core = harness.core
    host = await _with_host(harness)
    host.invites = [
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
            "entry_id": "i1",
            "subject": "Free sync",
            "organizer": "Maria",
            "organizer_address": "maria@bank.bg",
            "start": "2026-09-08T14:00:00+03:00",
            "end": "2026-09-08T14:30:00+03:00",
            "conflicts": [],
        },
    ]
    # What the host's free-slot walker returns for the decline: 14:00-14:30 on 08.09 is
    # free in the calendar AS OF NOW - the invite accepted below is what fills it.
    host.slots_override = [
        {"start": "2026-09-08T14:00:00+03:00", "end": "2026-09-08T14:30:00+03:00"},
        {"start": "2026-09-09T09:00:00+03:00", "end": "2026-09-09T09:30:00+03:00"},
    ]
    harness.enable(
        rsvp=MeetingRsvpSettings(host="laptop", account="Work", allowed_domains=["bank.bg"], propose_slots=2)
    )

    dry = await core.rsvp.run_once(dry_run=True)
    assert [(d.subject, d.decision) for d in dry.decisions] == [("Clash", "decline"), ("Free sync", "accept")]
    assert dry.decisions[0].proposals == ["ср 09.09 09:00-09:30"], "the slot the later accept fills must not be offered"

    live = await core.rsvp.run_once()
    assert live.errors == []
    assert [(r[0], r[1]) for r in host.responses] == [("i2", "decline"), ("i1", "accept")]
    comment = host.responses[0][2]
    assert "14:00-14:30" not in comment
    assert "09.09 09:00-09:30" in comment
    state = await core.rsvp.state()
    assert state is not None and state.answered_total == 2 and state.last_error is None
