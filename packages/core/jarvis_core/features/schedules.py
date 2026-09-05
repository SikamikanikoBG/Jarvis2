"""Scheduled prompts — the replacement for V1's reminders/scheduler.

Each fire creates its own conversation (grouped under the schedule in the sidebar) and one
run. ``schedule_fires`` has a UNIQUE (schedule_id, scheduled_for) key, so a slot can fire once
and only once, whatever restarts or clock jumps happen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import ConversationKind, RunKind, Schedule, ScheduleFire, ToolResult, new_id
from jarvis_proto.events import ScheduleChanged
from jarvis_proto.runs import ThinkLevel

log = logging.getLogger(__name__)

TICK_S = 15.0

# fire(text, conversation_kind, folder_key, folder_label, title, kind, think, think_level) -> (run_id, conversation_id)
FireFn = Callable[..., Awaitable[tuple[str, str]]]


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def compute_next(cron: str | None, at: datetime | None, tz: str, after: datetime) -> datetime | None:
    if at is not None:
        # A one-shot keeps its slot even if it is already in the past: the ticker's catch-up
        # policy decides whether a late slot still fires; ``advance`` disables it afterwards.
        return at
    if cron:
        zone = ZoneInfo(tz)
        base = after.astimezone(zone)
        return croniter(cron, base).get_next(datetime).astimezone(UTC)
    return None


class ScheduleStore:
    def __init__(self, db: Database, bus: EventBus | None = None) -> None:
        self.db = db
        self.bus = bus

    def _changed(self, schedule_id: str | None) -> None:
        if self.bus is not None:
            self.bus.publish(ScheduleChanged(schedule_id=schedule_id))

    async def _row_to_schedule(self, row: Any) -> Schedule:
        fire = await self.db.fetchone(
            "SELECT f.run_id, r.status FROM schedule_fires f LEFT JOIN runs r ON r.id = f.run_id"
            " WHERE f.schedule_id = ? ORDER BY f.scheduled_for DESC LIMIT 1",
            (row["id"],),
        )
        return Schedule(
            id=row["id"],
            name=row["name"],
            prompt=row["prompt"],
            cron=row["cron"],
            at=_dt(row["at"]),
            tz=row["tz"],
            enabled=bool(row["enabled"]),
            catch_up=row["catch_up"],
            think=None if row["think"] is None else bool(row["think"]),
            think_level=row["think_level"],
            next_fire=_dt(row["next_fire"]),
            last_fired_for=_dt(row["last_fired_for"]),
            last_run_id=fire["run_id"] if fire else None,
            last_status=fire["status"] if fire else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def list(self) -> list[Schedule]:
        rows = await self.db.fetchall("SELECT * FROM schedules ORDER BY created_at")
        return [await self._row_to_schedule(r) for r in rows]

    async def get(self, schedule_id: str) -> Schedule | None:
        row = await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return await self._row_to_schedule(row) if row else None

    async def create(
        self,
        *,
        name: str,
        prompt: str,
        cron: str | None = None,
        at: datetime | None = None,
        tz: str = "Europe/Sofia",
        enabled: bool = True,
        catch_up: str = "skip",
        think: bool | None = None,
        think_level: ThinkLevel | None = None,
    ) -> Schedule:
        if bool(cron) == bool(at):
            raise ValueError("exactly one of cron or at is required")
        if cron and not croniter.is_valid(cron):
            raise ValueError(f"invalid cron expression {cron!r}")
        ZoneInfo(tz)  # validates
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=ZoneInfo(tz))
        now = _now()
        next_fire = compute_next(cron, at, tz, now)
        sid = new_id("sch")
        await self.db.execute(
            "INSERT INTO schedules(id, name, prompt, cron, at, tz, enabled, catch_up, think, think_level, next_fire,"
            " last_fired_for, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                sid,
                name.strip(),
                prompt.strip(),
                cron,
                at.isoformat() if at else None,
                tz,
                int(enabled),
                catch_up,
                None if think is None else int(think),
                think_level,
                next_fire.isoformat() if next_fire else None,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self._changed(sid)
        schedule = await self.get(sid)
        assert schedule is not None
        return schedule

    async def update(self, schedule_id: str, **fields: Any) -> Schedule | None:
        current = await self.get(schedule_id)
        if current is None:
            return None
        data = current.model_dump()
        for key, value in fields.items():
            if (
                key in {"name", "prompt", "cron", "at", "tz", "enabled", "catch_up", "think", "think_level"}
                and value is not None
            ):
                data[key] = value
        if "cron" in fields and fields["cron"] is not None:
            data["at"] = None
        if "at" in fields and fields["at"] is not None:
            data["cron"] = None
        if bool(data["cron"]) == bool(data["at"]):
            raise ValueError("exactly one of cron or at is required")
        if data["cron"] and not croniter.is_valid(data["cron"]):
            raise ValueError(f"invalid cron expression {data['cron']!r}")
        at = data["at"]
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=ZoneInfo(data["tz"]))
        next_fire = compute_next(data["cron"], at, data["tz"], _now()) if data["enabled"] else None
        await self.db.execute(
            "UPDATE schedules SET name=?, prompt=?, cron=?, at=?, tz=?, enabled=?, catch_up=?, think=?, think_level=?,"
            " next_fire=?, updated_at=? WHERE id=?",
            (
                data["name"].strip(),
                data["prompt"].strip(),
                data["cron"],
                at.isoformat() if at else None,
                data["tz"],
                int(data["enabled"]),
                data["catch_up"],
                None if data["think"] is None else int(data["think"]),
                data["think_level"],
                next_fire.isoformat() if next_fire else None,
                _now().isoformat(),
                schedule_id,
            ),
        )
        self._changed(schedule_id)
        return await self.get(schedule_id)

    async def delete(self, schedule_id: str) -> None:
        await self.db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        self._changed(schedule_id)

    async def fires(self, schedule_id: str, *, limit: int = 20) -> list[ScheduleFire]:
        rows = await self.db.fetchall(
            "SELECT f.*, r.status FROM schedule_fires f LEFT JOIN runs r ON r.id = f.run_id"
            " WHERE f.schedule_id = ? ORDER BY f.scheduled_for DESC LIMIT ?",
            (schedule_id, limit),
        )
        return [
            ScheduleFire(
                schedule_id=r["schedule_id"],
                scheduled_for=datetime.fromisoformat(r["scheduled_for"]),
                run_id=r["run_id"],
                conversation_id=r["conversation_id"],
                status=r["status"],
            )
            for r in rows
        ]

    async def claim_slot(self, schedule_id: str, scheduled_for: datetime) -> bool:
        """True if this (schedule, slot) was free and is now ours."""
        try:
            await self.db.execute(
                "INSERT INTO schedule_fires(schedule_id, scheduled_for, created_at) VALUES (?,?,?)",
                (schedule_id, scheduled_for.isoformat(), _now().isoformat()),
            )
        except Exception:
            return False
        return True

    async def record_fire(self, schedule_id: str, scheduled_for: datetime, run_id: str, conversation_id: str) -> None:
        await self.db.execute(
            "UPDATE schedule_fires SET run_id = ?, conversation_id = ? WHERE schedule_id = ? AND scheduled_for = ?",
            (run_id, conversation_id, schedule_id, scheduled_for.isoformat()),
        )

    async def advance(self, schedule: Schedule, fired_for: datetime | None) -> None:
        after = max(_now(), fired_for) if fired_for else _now()
        if schedule.at is not None:
            # One-shot: it fired (or was skipped) — disable.
            await self.db.execute(
                "UPDATE schedules SET enabled = 0, next_fire = NULL, last_fired_for = COALESCE(?, last_fired_for), updated_at = ? WHERE id = ?",
                (fired_for.isoformat() if fired_for else None, _now().isoformat(), schedule.id),
            )
        else:
            next_fire = compute_next(schedule.cron, None, schedule.tz, after)
            await self.db.execute(
                "UPDATE schedules SET next_fire = ?, last_fired_for = COALESCE(?, last_fired_for), updated_at = ? WHERE id = ?",
                (
                    next_fire.isoformat() if next_fire else None,
                    fired_for.isoformat() if fired_for else None,
                    _now().isoformat(),
                    schedule.id,
                ),
            )
        self._changed(schedule.id)

    async def due(self, now: datetime) -> list[Schedule]:
        rows = await self.db.fetchall(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_fire IS NOT NULL AND next_fire <= ? ORDER BY next_fire",
            (now.isoformat(),),
        )
        return [await self._row_to_schedule(r) for r in rows]


class Scheduler:
    """Ticks every TICK_S; fires due schedules through the engine; applies catch-up policy."""

    def __init__(self, store: ScheduleStore, fire: FireFn, *, tick_s: float = TICK_S) -> None:
        self.store = store
        self._fire = fire
        self._tick_s = tick_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.tick()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    async def tick(self, now: datetime | None = None) -> int:
        now = now or _now()
        fired = 0
        for schedule in await self.store.due(now):
            slot = schedule.next_fire
            if slot is None:
                continue
            late_by = (now - slot).total_seconds()
            missed = late_by > 2 * self._tick_s + 60
            if missed and schedule.catch_up == "skip":
                log.info("schedule %s: slot %s missed by %.0fs, skipping (catch_up=skip)", schedule.name, slot, late_by)
                await self.store.advance(schedule, None)
                continue
            if not await self.store.claim_slot(schedule.id, slot):
                await self.store.advance(schedule, slot)
                continue
            try:
                run_id, conv_id = await self.fire_now(schedule, slot)
                await self.store.record_fire(schedule.id, slot, run_id, conv_id)
                fired += 1
            except Exception:
                log.exception("schedule %s failed to fire", schedule.name)
            await self.store.advance(schedule, slot)
        return fired

    async def fire_now(self, schedule: Schedule, slot: datetime | None = None) -> tuple[str, str]:
        slot = slot or _now()
        local = slot.astimezone(ZoneInfo(schedule.tz)).strftime("%Y-%m-%d %H:%M")
        return await self._fire(
            text=schedule.prompt,
            conversation_kind=ConversationKind.SCHEDULED,
            folder_key=schedule.id,
            folder_label=schedule.name,
            title=f"{schedule.name} · {local}",
            kind=RunKind.SCHEDULED,
            think=schedule.think,
            think_level=schedule.think_level,
        )


# --- tools ------------------------------------------------------------------------------


class _CreateArgs(BaseModel):
    name: str = Field(description="Short name, e.g. 'Morning brief'")
    prompt: str = Field(description="What Jarvis should do each time it fires")
    cron: str | None = Field(default=None, description="5-field cron in the user's timezone, e.g. '0 8 * * 1-5'")
    at: str | None = Field(default=None, description="ISO datetime for a one-shot, e.g. '2026-09-06T15:00'")


class _IdArgs(BaseModel):
    id: str


class ScheduleTools(BuiltinProvider):
    name = "schedule"

    def __init__(self, store: ScheduleStore, *, tz: Callable[[], str]) -> None:
        self.store = store
        self._tz = tz
        super().__init__()

    @tool(
        "schedule.list",
        description="List scheduled prompts with their next fire time.",
        read_only=True,
        idempotent=True,
    )
    async def _list(self) -> ToolResult:
        items = await self.store.list()
        if not items:
            return ToolResult.empty("No schedules.")
        return ToolResult.data(
            "\n".join(
                f"- [{s.id}] {s.name} — {s.cron or ('once at ' + s.at.isoformat() if s.at else '?')}"
                f" — next {s.next_fire.isoformat() if s.next_fire else 'never'}{'' if s.enabled else ' (disabled)'}"
                for s in items
            ),
            count=len(items),
        )

    @tool(
        "schedule.create",
        description=(
            "Create a NEW scheduled prompt (recurring cron or one-shot at) when Arsen asks for a future or "
            "recurring reminder/job. Never call it from inside a scheduled run - that run already IS the schedule."
        ),
        args=_CreateArgs,
        destructive=True,
    )
    async def _create(self, name: str, prompt: str, cron: str | None = None, at: str | None = None) -> ToolResult:
        try:
            at_dt = datetime.fromisoformat(at) if at else None
            s = await self.store.create(name=name, prompt=prompt, cron=cron, at=at_dt, tz=self._tz())
        except (ValueError, TypeError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.data(
            f"Scheduled {s.name!r} ({s.id}); next fire {s.next_fire.isoformat() if s.next_fire else 'never'}."
        )

    @tool("schedule.delete", description="Delete a scheduled prompt by id.", args=_IdArgs, destructive=True)
    async def _delete(self, id: str) -> ToolResult:
        if await self.store.get(id) is None:
            return ToolResult.failure(f"no schedule {id!r}")
        await self.store.delete(id)
        return ToolResult.data(f"Deleted {id}.")
