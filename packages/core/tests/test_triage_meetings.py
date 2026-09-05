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
from jarvis_proto import RunStatus, ToolResult, TriageSettings
from tests.conftest import Harness


class _ListArgs(BaseModel):
    account: str
    folder: str = "Inbox"
    since: str | None = None
    limit: int = 50


class _MoveArgs(BaseModel):
    entry_id: str
    folder: str


class _MeetingArgs(BaseModel):
    meeting_id: str
    after_seq: int = 0


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
        ]
        self.pulled = 0
        super().__init__()

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
    # Classifier answers for the two non-demand mails: lunch → none, invoice → invoices.
    harness.chat.push(FakeTurn(text='{"category": "none"}'), FakeTurn(text='{"category": "invoices"}'))
    global_sub = harness.subscribe("none")
    report = await core.triage.run_once()
    assert report.errors == [] and report.processed == 3 and report.routed == 2
    assert host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    # Persisted decisions: a second run re-does nothing and moves nothing.
    report2 = await core.triage.run_once()
    assert report2.processed == 0 and host.moves == [("e1", "Demands/DM-4521"), ("e3", "Finance/Invoices")]
    states = await core.triage.states()
    assert states[0].account == "Work" and states[0].processed_today == 3 and states[0].cursor == "2026-09-05T08:10:00"
    # The day's triage conversation got one compact message.
    convs = [c for c in await core.store.list_conversations() if c.kind.value == "triage"]
    assert len(convs) == 1 and convs[0].folder_key == "Work"
    msgs = await core.store.list_messages(convs[0].id)
    # The summary names the sender: `from` dict for e1, address-only fallback for e3.
    assert msgs[-1].name == "triage" and "Rumen Petrov" in msgs[-1].content and "Demands/DM-4521" in msgs[-1].content
    assert "billing@vendor.com" in msgs[-1].content
    assert any(getattr(e, "type", "") == "conversation.updated" for e in _drain(global_sub))


async def test_triage_without_host_reports_instead_of_crashing(harness: Harness):
    harness.enable(triage=TriageSettings(enabled=True, host=""))
    report = await harness.core.triage.run_once()
    assert report.errors and "triage.host" in report.errors[0]


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
