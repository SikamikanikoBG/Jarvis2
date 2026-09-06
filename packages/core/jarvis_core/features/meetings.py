"""Meetings: capture on a host, transcribe in the core, summarise with a run.

Audio reaches the core either pushed by the host (``POST /api/meetings/{id}/chunks``) or pulled
by the core from the host's ``meeting_pull`` tool while recording. Each chunk becomes
transcript segments (events + one ``transcript`` message in the meeting's conversation), so the
summary run has the whole transcript as ordinary context.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis_proto import ConversationKind, Meeting, MeetingDetail, Message, RunKind, new_id
from jarvis_proto.events import ConversationUpdated, MeetingChanged, MeetingSegment, MessageCreated
from jarvis_proto.features import MeetingFrameModel, MeetingSegmentModel, MeetingStatus

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    "Summarise the meeting transcript above for {user}. Sections: Decisions; Action items (owner, "
    "what, when); Open questions; Key numbers and facts. Be concrete, use the participants' words for "
    "commitments, and say what was unclear rather than guessing. If frames were captured, reference "
    "them by time where they matter."
)

PULL_INTERVAL_S = 10.0


class MeetingService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._pollers: dict[str, asyncio.Task[None]] = {}
        self._watchers: set[asyncio.Task[None]] = set()

    # --- store ---------------------------------------------------------------------------

    @staticmethod
    def _row(r: Any) -> Meeting:
        return Meeting(
            id=r["id"],
            conversation_id=r["conversation_id"],
            title=r["title"],
            host=r["host"],
            status=MeetingStatus(r["status"]),
            started_at=datetime.fromisoformat(r["started_at"]),
            ended_at=datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else None,
            summary_run_id=r["summary_run_id"],
        )

    async def list(self) -> list[Meeting]:
        rows = await self.core.db.fetchall("SELECT * FROM meetings ORDER BY started_at DESC")
        return [self._row(r) for r in rows]

    async def get(self, meeting_id: str) -> Meeting | None:
        row = await self.core.db.fetchone("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        return self._row(row) if row else None

    async def detail(self, meeting_id: str) -> MeetingDetail | None:
        m = await self.get(meeting_id)
        if m is None:
            return None
        segs = await self.core.db.fetchall(
            "SELECT seq, t0, t1, text FROM meeting_segments WHERE meeting_id = ? ORDER BY seq", (meeting_id,)
        )
        frames = await self.core.db.fetchall(
            "SELECT seq, at, path, ocr FROM meeting_frames WHERE meeting_id = ? ORDER BY seq", (meeting_id,)
        )
        return MeetingDetail(
            **m.model_dump(),
            segments=[MeetingSegmentModel(seq=s["seq"], t0=s["t0"], t1=s["t1"], text=s["text"]) for s in segs],
            frames=[
                MeetingFrameModel(
                    seq=f["seq"], at=f["at"], url=f"/api/meetings/{meeting_id}/frames/{f['seq']}", ocr=f["ocr"]
                )
                for f in frames
            ],
        )

    async def _set_status(self, meeting: Meeting, status: MeetingStatus, **fields: Any) -> Meeting:
        ended = fields.get("ended_at")
        await self.core.db.execute(
            "UPDATE meetings SET status = ?, ended_at = COALESCE(?, ended_at), summary_run_id = COALESCE(?, summary_run_id) WHERE id = ?",
            (status.value, ended.isoformat() if ended else None, fields.get("summary_run_id"), meeting.id),
        )
        updated = await self.get(meeting.id)
        assert updated is not None
        self.core.bus.publish(
            MeetingChanged(meeting_id=meeting.id, conversation_id=meeting.conversation_id, status=status.value)
        )
        return updated

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self, *, title: str | None, host: str) -> Meeting:
        now = datetime.now(UTC)
        title = (title or "Meeting").strip()
        conv = await self.core.store.create_conversation(
            kind=ConversationKind.MEETING,
            title=f"{now.astimezone().strftime('%Y-%m-%d %H:%M')} {title}",
            folder_key=host,
            folder_label=host,
        )
        self.core.bus.publish(ConversationUpdated(conversation=conv))
        meeting = Meeting(id=new_id("mtg"), conversation_id=conv.id, title=title, host=host, started_at=now)
        await self.core.db.execute(
            "INSERT INTO meetings(id, conversation_id, title, host, status, started_at) VALUES (?,?,?,?,?,?)",
            (meeting.id, conv.id, title, host, meeting.status.value, now.isoformat()),
        )
        res = await self.core.registry.call(
            f"{host}.meeting_start",
            {"meeting_id": meeting.id},
            cancel=asyncio.Event(),
            idempotency_key=f"mtg:{meeting.id}:start",
            timeout_s=60,
        )
        if res.kind.value == "error":
            await self._set_status(meeting, MeetingStatus.FAILED, ended_at=now)
            raise RuntimeError(f"host could not start capture: {res.text[:200]}")
        self.core.bus.publish(
            MeetingChanged(meeting_id=meeting.id, conversation_id=conv.id, status=meeting.status.value)
        )
        self._pollers[meeting.id] = asyncio.create_task(self._poll(meeting), name=f"meeting-{meeting.id}")
        return meeting

    async def stop(self, meeting_id: str) -> Meeting:
        meeting = await self.get(meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        if meeting.status is not MeetingStatus.RECORDING:
            return meeting
        poller = self._pollers.pop(meeting.id, None)
        if poller:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
        await self.core.registry.call(
            f"{meeting.host}.meeting_stop",
            {"meeting_id": meeting.id},
            cancel=asyncio.Event(),
            idempotency_key=f"mtg:{meeting.id}:stop",
            timeout_s=60,
        )
        await self._pull_once(meeting)  # drain what the host still holds
        meeting = await self._set_status(meeting, MeetingStatus.SUMMARISING, ended_at=datetime.now(UTC))
        row = await self.core.db.fetchone(
            "SELECT COUNT(*) AS n FROM meeting_segments WHERE meeting_id = ?", (meeting.id,)
        )
        if not row or not int(row["n"]):
            # Nothing was heard. Asking the model to "summarise the transcript above" with no
            # transcript makes it summarise something else it remembers - the first silent
            # recording came back as a summary of an unrelated Teams call from August.
            return await self._nothing_recorded(meeting)
        run, _ = await self.core.engine.create_run(
            text=_SUMMARY_PROMPT.format(user=self.core.settings.user_name),
            conversation_id=meeting.conversation_id,
            kind=RunKind.MEETING,
        )
        meeting = await self._set_status(meeting, MeetingStatus.SUMMARISING, summary_run_id=run.id)
        # Keep the reference: a bare create_task can be garbage-collected mid-flight.
        watcher = asyncio.create_task(self._finish_when_done(meeting, run.id), name=f"meeting-finish-{meeting.id}")
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return meeting

    async def _nothing_recorded(self, meeting: Meeting) -> Meeting:
        """Say so plainly instead of inventing a summary. No run, no model call."""
        text = (
            "No speech was captured, so there is nothing to summarise. The recording ran but every "
            "chunk was silence — check that the right microphone is active and, for a call, that "
            "system audio is being played through the speakers this machine captures."
        )
        msg = await self.core.store.add_message(
            Message.assistant(text, conversation_id=meeting.conversation_id, name="meeting")
        )
        self.core.bus.publish(MessageCreated(message=msg))
        conv = await self.core.store.get_conversation(meeting.conversation_id)
        if conv:
            self.core.bus.publish(ConversationUpdated(conversation=conv))
        return await self._set_status(meeting, MeetingStatus.DONE)

    async def _finish_when_done(self, meeting: Meeting, run_id: str) -> None:
        try:
            async with asyncio.timeout(1800):
                while True:
                    run = await self.core.store.get_run(run_id)
                    if run is not None and run.status.terminal:
                        break
                    await asyncio.sleep(1.0)
            ok = run.status.value == "done"
            await self._set_status(meeting, MeetingStatus.DONE if ok else MeetingStatus.FAILED)
        except Exception:
            await self._set_status(meeting, MeetingStatus.FAILED)

    # --- audio -----------------------------------------------------------------------------

    async def _poll(self, meeting: Meeting) -> None:
        while True:
            await asyncio.sleep(PULL_INTERVAL_S)
            try:
                await self._pull_once(meeting)
            except Exception as exc:
                log.warning("meeting %s pull failed: %s", meeting.id, exc)

    async def _pull_once(self, meeting: Meeting) -> None:
        last = await self.core.db.fetchone(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM meeting_segments WHERE meeting_id = ?", (meeting.id,)
        )
        res = await self.core.registry.call(
            f"{meeting.host}.meeting_pull",
            {"meeting_id": meeting.id, "after_seq": int(last["s"]) if last else 0},
            cancel=asyncio.Event(),
            idempotency_key=f"mtg:{meeting.id}:pull",
            timeout_s=60,
        )
        if res.kind.value in {"error", "empty"}:
            return
        try:
            data = json.loads(res.text)
        except json.JSONDecodeError:
            return
        for chunk in data.get("chunks", []) if isinstance(data, dict) else []:
            audio = base64.b64decode(chunk.get("wav_base64", ""))
            if audio:
                await self.ingest_chunk(
                    meeting.id, audio, float(chunk.get("t0", 0.0)), filename="chunk.wav", mime="audio/wav"
                )

    async def ingest_chunk(self, meeting_id: str, audio: bytes, t0: float, *, filename: str, mime: str) -> int:
        meeting = await self.get(meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        result = await self.core.transcriber.transcribe(audio, filename=filename, mime=mime)
        segments = result.segments or ([{"t0": 0.0, "t1": None, "text": result.text}] if result.text else [])
        added = 0
        lines: list[str] = []
        for seg in segments:
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            s0 = t0 + float(seg.get("t0") or 0.0)
            s1 = t0 + float(seg.get("t1") or seg.get("t0") or 0.0)
            row = await self.core.db.fetchone(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM meeting_segments WHERE meeting_id = ?", (meeting_id,)
            )
            seq = int(row["n"]) if row else 1
            await self.core.db.execute(
                "INSERT INTO meeting_segments(meeting_id, seq, t0, t1, text) VALUES (?,?,?,?,?)",
                (meeting_id, seq, s0, s1, text),
            )
            self.core.bus.publish(
                MeetingSegment(
                    meeting_id=meeting_id, conversation_id=meeting.conversation_id, seq=seq, t0=s0, t1=s1, text=text
                )
            )
            lines.append(f"[{_mmss(s0)}] {text}")
            added += 1
        if lines:
            msg = await self.core.store.add_message(
                Message.user("\n".join(lines), conversation_id=meeting.conversation_id, name="transcript")
            )
            self.core.bus.publish(MessageCreated(message=msg))
        return added

    # --- frames -----------------------------------------------------------------------------

    def frames_dir(self, meeting_id: str) -> Path:
        d = self.core.config.home / "meetings" / meeting_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def add_frame(self, meeting_id: str, png: bytes, at: float, *, ocr: str | None = None) -> int:
        meeting = await self.get(meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        row = await self.core.db.fetchone(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM meeting_frames WHERE meeting_id = ?", (meeting_id,)
        )
        seq = int(row["n"]) if row else 1
        path = self.frames_dir(meeting_id) / f"frame_{seq:04d}.png"
        path.write_bytes(png)
        await self.core.db.execute(
            "INSERT INTO meeting_frames(meeting_id, seq, at, path, ocr) VALUES (?,?,?,?,?)",
            (meeting_id, seq, at, str(path), ocr),
        )
        if ocr:
            msg = await self.core.store.add_message(
                Message.user(
                    f"[frame {seq} at {_mmss(at)}] {ocr[:1500]}", conversation_id=meeting.conversation_id, name="frame"
                )
            )
            self.core.bus.publish(MessageCreated(message=msg))
        return seq

    async def frame_path(self, meeting_id: str, seq: int) -> Path | None:
        row = await self.core.db.fetchone(
            "SELECT path FROM meeting_frames WHERE meeting_id = ? AND seq = ?", (meeting_id, seq)
        )
        return Path(row["path"]) if row else None

    async def stop_all(self) -> None:
        for task in list(self._pollers.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._pollers.clear()


def _mmss(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 60:02d}:{s % 60:02d}"
