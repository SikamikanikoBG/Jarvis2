"""Episodes for the days before there were episodes (memory-track PROPOSAL §6.3).

The day journal writes an episode when a run ends (features/episodes.py), so memory began on
2026-11-24: of the eleven weeks before it, Jarvis keeps the messages and nothing a person would
remember. This writes those weeks up the same way, at night: the same question the live journal
asks, over the same runs, oldest day first, so a talk that went on over several runs is one
episode, as it would have been. Only episodes are written - a day's digest stays as it was written
that day, and no message is touched.

It is background work on the GPU that answers Arsen, so it is careful about when: only inside
``assistant.backfill_hours`` of his night, never while a chat or call of his is running or has
started in the last ``QUIET_S``, one call at a time (it shares the live journal's turn), and it
looks again before every call. How far it got is kept per conversation and day
(``episode_backfill``), so a restart goes on where it stopped and a routine day is asked once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis_core.features.journal import RUNS_MAX, SKIPPED_KINDS, ModelUnavailable
from jarvis_proto import Run, RunKind, RunStatus

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

#: How often it looks whether it may work.
INTERVAL_S = 60.0
#: His last chat or call began at least this long ago.
QUIET_S = 30 * 60
#: A batch whose answer cannot be read is asked again this many times, then passed over: one
#: broken answer must not hold the rest of history back.
MAX_TRIES = 3
#: After an answer that could not be read, or none at all (the server is down), wait this long.
#: A server that does not answer costs no try: on 2026-11-28 vader was off the network all night,
#: and counting each refused connection as an unreadable answer passed five days over unwritten.
PAUSE_S = 15 * 60

_WRITTEN = (RunStatus.DONE, RunStatus.FAILED, RunStatus.WAITING_USER)
_BUSY = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLING)


def _at(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def in_hours(hour: int, hours: tuple[int, int]) -> bool:
    """``hour`` within [from, until); a window may run over midnight (22-5)."""
    start, end = hours
    return start <= hour < end if start < end else (hour >= start or hour < end)


class EpisodeBackfill:
    def __init__(self, core: Core, *, interval_s: float = INTERVAL_S) -> None:
        self.core = core
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._paused_until: datetime | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="episode-backfill")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.work()
            except Exception:
                log.exception("episode back-fill failed")

    # --- when ---------------------------------------------------------------------------------

    async def may_work(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        s = self.core.settings
        if not (s.assistant.journal and s.assistant.backfill):
            return False
        if self._paused_until is not None and now < self._paused_until:
            return False
        if not in_hours(now.astimezone(ZoneInfo(s.timezone)).hour, s.assistant.backfill_hours):
            return False
        busy = ",".join("?" for _ in _BUSY)
        row = await self.core.db.fetchone(
            f"SELECT MAX(created_at) AS last, SUM(status IN ({busy})) AS busy FROM runs WHERE kind = ?",
            (*(st.value for st in _BUSY), RunKind.CHAT.value),
        )
        if row is None or row["last"] is None:
            return True
        return not row["busy"] and now - _at(row["last"]) >= timedelta(seconds=QUIET_S)

    # --- what ---------------------------------------------------------------------------------

    async def work(self, now: datetime | None = None) -> int:
        """Write up batches while it may; returns how many were asked."""
        asked = 0
        while await self.may_work(now):
            unit = await self._next(now)
            if unit is None:
                return asked
            ok = await self._write(*unit)
            asked += 1
            if not ok:
                self._paused_until = (now or datetime.now(UTC)) + timedelta(seconds=PAUSE_S)
                return asked
        return asked

    async def _next(self, now: datetime | None) -> tuple[Any, date, list[Run], Any] | None:
        """The oldest day not finished yet: its conversation, the day, the runs still to write up
        (oldest first) and its progress row. A day with nothing left is marked finished on the way."""
        s = self.core.settings
        tz = ZoneInfo(s.timezone)
        today = (now or datetime.now(UTC)).astimezone(tz).date()
        every = await self.core.store.runs_oldest_first(
            _WRITTEN,
            not_run_kinds=(RunKind.TRIAGE.value,),
            not_conversation_kinds=tuple(k.value for k in SKIPPED_KINDS),
        )
        progress = {
            (p["conversation_id"], p["day"]): p for p in await self.core.db.fetchall("SELECT * FROM episode_backfill")
        }
        days: dict[tuple[str, str], list[Run]] = {}
        for run in every:
            day = run.created_at.astimezone(tz).date()
            if day >= today:  # today belongs to the live journal
                continue
            key = (run.conversation_id, day.isoformat())
            mark = progress.get(key)
            if mark is not None and mark["done"]:
                continue
            days.setdefault(key, []).append(run)
        for (cid, iso), of_day in days.items():
            mark = progress.get((cid, iso))
            runs = of_day
            if mark is not None and mark["last_run_id"]:
                ids = [r.id for r in of_day]
                if mark["last_run_id"] in ids:
                    runs = of_day[ids.index(mark["last_run_id"]) + 1 :]
            # What the live journal already wrote up that day (the day episodes began) is not asked twice.
            told = await self.core.journal.episodes.of_day(cid, iso)
            runs = [r for r in runs if not any(e.started_at <= r.created_at <= e.ended_at for e in told)]
            conv = await self.core.store.get_conversation(cid)
            if not runs or conv is None:
                await self._mark(cid, iso, mark["last_run_id"] if mark else None, tries=0, done=True)
                continue
            return conv, date.fromisoformat(iso), runs, mark
        return None

    async def _write(self, conv: Any, day: date, runs: list[Run], mark: Any) -> bool:
        batch = runs[:RUNS_MAX]
        done = len(runs) <= RUNS_MAX
        try:
            written = await self.core.journal.past_episode(conv, batch, day)
        except ModelUnavailable as exc:
            log.info("episode back-fill: model unavailable (%s); waiting", exc)
            return False
        if written:
            await self._mark(conv.id, day.isoformat(), batch[-1].id, tries=0, done=done)
            return True
        tries = (mark["tries"] if mark else 0) + 1
        if tries >= MAX_TRIES:
            log.warning("episode back-fill: passed over %s %s after %d unreadable answers", conv.id, day, tries)
            await self._mark(conv.id, day.isoformat(), batch[-1].id, tries=0, done=done)
        else:
            await self._mark(conv.id, day.isoformat(), mark["last_run_id"] if mark else None, tries=tries, done=False)
        return False

    async def _mark(self, cid: str, day: str, last_run_id: str | None, *, tries: int, done: bool) -> None:
        await self.core.db.execute(
            "INSERT INTO episode_backfill(conversation_id, day, last_run_id, tries, done, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(conversation_id, day) DO UPDATE SET last_run_id = excluded.last_run_id,"
            " tries = excluded.tries, done = excluded.done, updated_at = excluded.updated_at",
            (cid, day, last_run_id, tries, int(done), datetime.now(UTC).isoformat()),
        )
