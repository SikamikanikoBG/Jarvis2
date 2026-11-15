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
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.engine.current import current_conversation_id
from jarvis_core.tools.builtin import BuiltinProvider, CallRisk, tool
from jarvis_proto import (
    ChangeField,
    ChangeRecord,
    ConversationKind,
    RunKind,
    Schedule,
    ScheduleFire,
    ToolResult,
    new_id,
)
from jarvis_proto.events import ScheduleChanged
from jarvis_proto.features import CatchUp, ScheduleCallMe
from jarvis_proto.runs import ThinkLevel

if TYPE_CHECKING:
    from jarvis_core.app import Core
    from jarvis_core.features.admin import Who

log = logging.getLogger(__name__)

TICK_S = 15.0

# fire(text, conversation_kind, folder_key, folder_label, title, kind, think, think_level, call_me) -> (run_id, conversation_id)
FireFn = Callable[..., Awaitable[tuple[str, str]]]


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def compute_next(cron: str | None, at: datetime | None, tz: str, after: datetime) -> datetime | None:
    if at is not None:
        # A one-shot keeps its slot even if it is already in the past: the ticker's catch-up
        # policy decides whether a late slot still fires; ``advance`` disables it afterwards.
        # Stored as UTC like every other next_fire: kept in its own offset ("+02:00"), the due
        # check compared it as text against UTC and a one-off fired two hours late, or never
        # (2026-10-30, production's "Messenger РД"). A naive time is the schedule's wall clock.
        return (at if at.tzinfo else at.replace(tzinfo=ZoneInfo(tz))).astimezone(UTC)
    if cron:
        zone = ZoneInfo(tz)
        it = croniter(cron, after.astimezone(zone))
        # Like cron: a schedule set to certain hours fires once at a wall-clock time the clocks
        # go back over (its first occurrence); one with every hour keeps to real time.
        fixed_hours = it.expanded[1] != ["*"]
        while True:
            nxt = it.get_next(datetime)
            if not (fixed_hours and _repeated_wall_time(nxt, zone)):
                return nxt.astimezone(UTC)
    return None


def _repeated_wall_time(value: datetime, zone: ZoneInfo) -> bool:
    """True when ``value`` is the second occurrence of a local time that happens twice."""
    wall = value.replace(tzinfo=None)
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    return first.utcoffset() != second.utcoffset() and value.astimezone(UTC) == second.astimezone(UTC)


# A schedule's prompt is a standing instruction, not a document: generous, but bounded (EC-10).
PROMPT_MAX_CHARS = 20_000
# A one-shot given a time this far in the past is a mistake, not a catch-up.
_PAST_GRACE_S = 60
_EDITABLE = {"name", "prompt", "cron", "at", "tz", "enabled", "catch_up", "call_me"}
_CLEARABLE = {"think", "think_level"}


def _not_past(at: datetime | None, now: datetime) -> None:
    if at is not None and (now - at).total_seconds() > _PAST_GRACE_S:
        raise ValueError(f"'at' {at.isoformat()} is in the past")


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
            call_me=row["call_me"],
            prompt_by=row["prompt_by"],
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
        call_me: ScheduleCallMe = "default",
        prompt_by: str | None = None,
    ) -> Schedule:
        if not name.strip() or not prompt.strip():
            raise ValueError("name and prompt are required")
        if len(prompt) > PROMPT_MAX_CHARS:
            raise ValueError(f"the prompt is {len(prompt)} characters; at most {PROMPT_MAX_CHARS}")
        if bool(cron) == bool(at):
            raise ValueError("exactly one of cron or at is required")
        if cron and not croniter.is_valid(cron):
            raise ValueError(f"invalid cron expression {cron!r}")
        ZoneInfo(tz)  # validates
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=ZoneInfo(tz))
        now = _now()
        _not_past(at, now)
        next_fire = compute_next(cron, at, tz, now)
        sid = new_id("sch")
        await self.db.execute(
            "INSERT INTO schedules(id, name, prompt, cron, at, tz, enabled, catch_up, think, think_level, call_me,"
            " prompt_by, next_fire, last_fired_for, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
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
                call_me,
                prompt_by,
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
        """Change a schedule in place: the same id, its history kept. A field left out (or None)
        stays as it is - except ``think`` and ``think_level``, where an explicit None means "back to
        the default" (it could never be cleared before 2026-11-15, in the UI either). ``prompt_by``
        is who wrote the prompt when it changes (None = Arsen)."""
        current = await self.get(schedule_id)
        if current is None:
            return None
        data = current.model_dump()
        for key, value in fields.items():
            if key in _CLEARABLE or (key in _EDITABLE and value is not None):
                data[key] = value
        if "prompt_by" in fields:
            data["prompt_by"] = fields["prompt_by"]
        if not str(data["name"]).strip() or not str(data["prompt"]).strip():
            raise ValueError("name and prompt may not be empty")
        if len(data["prompt"]) > PROMPT_MAX_CHARS:
            raise ValueError(f"the prompt is {len(data['prompt'])} characters; at most {PROMPT_MAX_CHARS}")
        ZoneInfo(data["tz"])
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
        if fields.get("at") is not None:
            _not_past(at, _now())  # a newly given time; a stored one-shot keeps its slot
        if data["think"] is not True:
            data["think_level"] = None if data["think"] is False else data["think_level"]
        next_fire = compute_next(data["cron"], at, data["tz"], _now()) if data["enabled"] else None
        await self.db.execute(
            "UPDATE schedules SET name=?, prompt=?, cron=?, at=?, tz=?, enabled=?, catch_up=?, think=?, think_level=?,"
            " call_me=?, prompt_by=?, next_fire=?, updated_at=? WHERE id=?",
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
                data["call_me"],
                data["prompt_by"],
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

    async def accept(self, schedule_id: str) -> Schedule | None:
        """Arsen accepts a prompt a run wrote: from now on it fires with a schedule's own authority."""
        await self.db.execute("UPDATE schedules SET prompt_by = NULL WHERE id = ?", (schedule_id,))
        self._changed(schedule_id)
        return await self.get(schedule_id)

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
        """Compared as instants, not as text: a next_fire stored before 2026-10-30 may carry its own
        offset, and "10:00+02:00" sorts after "09:30+00:00" although it is earlier."""
        rows = await self.db.fetchall("SELECT * FROM schedules WHERE enabled = 1 AND next_fire IS NOT NULL")
        due = [r for r in rows if (_dt(r["next_fire"]) or now) <= now]
        due.sort(key=lambda r: _dt(r["next_fire"]) or now)
        return [await self._row_to_schedule(r) for r in due]


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

    async def run_now(self, schedule: Schedule) -> tuple[str, str]:
        """The "Run now" button. Recorded like any other fire, so the schedule's history and its
        last_run_id show it — pressing it used to produce a run the schedule never heard about,
        leaving the card saying "last run: yesterday" next to a run that just finished."""
        slot = _now()
        await self.store.claim_slot(schedule.id, slot)
        run_id, conv_id = await self.fire_now(schedule, slot)
        await self.store.record_fire(schedule.id, slot, run_id, conv_id)
        return run_id, conv_id

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
            call_me=schedule.call_me,
        )


# --- tools ------------------------------------------------------------------------------

_THINK = Literal["on", "off", "default"]
_LEVEL = Literal["low", "medium", "high", "default"]


class _CreateArgs(BaseModel):
    name: str = Field(description="Short name, e.g. 'Morning brief'")
    prompt: str = Field(description="What Jarvis should do each time it fires")
    cron: str | None = Field(default=None, description="5-field cron in the user's timezone, e.g. '0 8 * * 1-5'")
    at: str | None = Field(default=None, description="ISO datetime for a one-shot, e.g. '2026-09-06T15:00'")
    tz: str | None = Field(default=None, description="IANA timezone; default the user's")
    enabled: bool = True
    catch_up: CatchUp = "skip"
    think: _THINK = "default"
    think_level: _LEVEL = "default"
    call_me: ScheduleCallMe = "default"


class _UpdateArgs(BaseModel):
    id: str
    name: str | None = None
    prompt: str | None = None
    cron: str | None = Field(default=None, description="Setting cron turns a one-shot into a recurring one")
    at: str | None = Field(default=None, description="ISO datetime; turns it into a one-shot")
    tz: str | None = None
    enabled: bool | None = Field(default=None, description="false = pause, true = resume")
    catch_up: CatchUp | None = None
    think: _THINK | None = None
    think_level: _LEVEL | None = None
    call_me: ScheduleCallMe | None = None
    reason: str = ""


class _IdArgs(BaseModel):
    id: str


class _GetArgs(BaseModel):
    id: str
    fires: int = Field(default=5, ge=0, le=50, description="How many recent fires to include")


class _FiresArgs(BaseModel):
    id: str
    limit: int = Field(default=20, ge=1, le=200)


# The fields a schedule change records and a revert puts back (prompt_by with the prompt).
CHANGE_FIELDS = (
    "name",
    "prompt",
    "cron",
    "at",
    "tz",
    "enabled",
    "catch_up",
    "think",
    "think_level",
    "call_me",
    "prompt_by",
)


def schedule_diff(before: Schedule | None, after: Schedule | None) -> list[ChangeField]:
    old = before.model_dump(mode="json") if before else {}
    new = after.model_dump(mode="json") if after else {}
    return [ChangeField(path=k, old=old.get(k), new=new.get(k)) for k in CHANGE_FIELDS if old.get(k) != new.get(k)]


def _think(value: str | None) -> tuple[bool, Any]:
    """(given, value) for think: "default" clears it, "on"/"off" set it."""
    if value is None:
        return False, None
    return True, {"on": True, "off": False}.get(value)


def _when(s: Schedule) -> str:
    return s.cron or ("once at " + s.at.isoformat() if s.at else "?")


def _local(value: datetime | None, tz: str) -> str:
    if value is None:
        return "never"
    return f"{value.astimezone(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M %Z')} ({value.astimezone(UTC).strftime('%H:%M')} UTC)"


class ScheduleTools(BuiltinProvider):
    """``schedule.*``: Jarvis's scheduled prompts, with everything the Schedules screen can do.

    Every write is recorded in the trail (features/admin.py) with the fields before and after, so
    a schedule edit can be reverted like a setting. A prompt written by a run nobody asked from
    keeps that authorship until Arsen accepts it (Schedule.prompt_by)."""

    name = "schedule"

    def __init__(
        self,
        store: ScheduleStore,
        *,
        tz: Callable[[], str],
        scheduler: Scheduler | None = None,
        core: Core | None = None,
    ) -> None:
        self.store = store
        self._tz = tz
        self._scheduler = scheduler
        self._core = core
        super().__init__()

    async def _who(self, idempotency_key: str = "") -> Who:
        from jarvis_core.features.admin import Who, who_now  # admin is wired after this module

        return await who_now(self._core, idempotency_key) if self._core is not None else Who()

    async def _record(
        self, who: Who, target: str, before: Schedule | None, after: Schedule | None, reason: str
    ) -> ChangeRecord | None:
        if self._core is None:
            return None
        fields = schedule_diff(before, after)
        if not fields:
            return None
        return await self._core.admin.changes.record(
            area="schedule",
            target=target,
            who=who,
            fields=fields,
            reason=reason,
            note="" if after is not None else "deleted; its past runs and their chats stay",
        )

    async def _own(self, schedule_id: str) -> bool:
        """The run asking is a fire of this very schedule (EC-7)."""
        conv_id = current_conversation_id.get()
        if self._core is None or conv_id is None:
            return False
        conv = await self._core.store.get_conversation(conv_id)
        return conv is not None and conv.folder_key == schedule_id

    async def assess(self, name: str, arguments: dict[str, Any]) -> CallRisk | None:
        if name != "schedule.delete":
            return None
        schedule = await self.store.get(str(arguments.get("id", "")))
        if schedule is None:
            return None
        fires = len(await self.store.fires(schedule.id, limit=1000))
        return CallRisk(
            reason=f"deletes the schedule {schedule.name!r} and its history of {fires} fire(s); the chats of past runs stay",
            risk=["delete"],
        )

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
                f"- [{s.id}] {s.name} — {_when(s)}"
                f" — next {s.next_fire.isoformat() if s.next_fire else 'never'}{'' if s.enabled else ' (paused)'}"
                for s in items
            ),
            count=len(items),
        )

    @tool(
        "schedule.get",
        description="One scheduled prompt in full: prompt, timing, options, next fire and its last fires.",
        args=_GetArgs,
        read_only=True,
        idempotent=True,
    )
    async def _get(self, id: str, fires: int = 5) -> ToolResult:
        s = await self.store.get(id)
        if s is None:
            return ToolResult.failure(f"no schedule {id!r}")
        lines = [
            f"[{s.id}] {s.name}" + ("" if s.enabled else " (paused)"),
            f"when: {_when(s)} ({s.tz}); next fire: {_local(s.next_fire, s.tz)}",
            f"catch_up={s.catch_up} think={'default' if s.think is None else s.think}"
            f" think_level={s.think_level or 'default'} call_me={s.call_me}",
        ]
        if s.prompt_by is not None:
            lines.append("prompt written by a run, not yet accepted by Arsen: it fires asking like a task")
        lines.append(f"prompt:\n{s.prompt}")
        history = await self.store.fires(s.id, limit=fires) if fires else []
        if history:
            lines.append("last fires:")
            lines += [
                f"- {_local(f.scheduled_for, s.tz)}: {f.status or 'not started'} (conversation {f.conversation_id})"
                for f in history
            ]
        return ToolResult.data("\n".join(lines))

    @tool(
        "schedule.fires",
        description="A schedule's fire history: when it fired, how each run ended, its conversation.",
        args=_FiresArgs,
        read_only=True,
        idempotent=True,
    )
    async def _fires(self, id: str, limit: int = 20) -> ToolResult:
        s = await self.store.get(id)
        if s is None:
            return ToolResult.failure(f"no schedule {id!r}")
        history = await self.store.fires(id, limit=limit)
        if not history:
            return ToolResult.empty(f"{s.name!r} has not fired yet.")
        return ToolResult.data(
            "\n".join(
                f"- {_local(f.scheduled_for, s.tz)}: {f.status or 'not started'} run {f.run_id} conversation {f.conversation_id}"
                for f in history
            ),
            count=len(history),
        )

    @tool(
        "schedule.create",
        description=(
            "Create a NEW scheduled prompt (recurring cron or one-shot at) when Arsen asks for a future or "
            "recurring reminder/job. Never call it from inside a scheduled run - that run already IS the schedule."
        ),
        args=_CreateArgs,
    )
    async def _create(
        self,
        name: str,
        prompt: str,
        cron: str | None = None,
        at: str | None = None,
        tz: str | None = None,
        enabled: bool = True,
        catch_up: CatchUp = "skip",
        think: str = "default",
        think_level: str = "default",
        call_me: ScheduleCallMe = "default",
        idempotency_key: str = "",
    ) -> ToolResult:
        who = await self._who(idempotency_key)
        try:
            at_dt = datetime.fromisoformat(at) if at else None
            s = await self.store.create(
                name=name,
                prompt=prompt,
                cron=cron,
                at=at_dt,
                tz=tz or self._tz(),
                enabled=enabled,
                catch_up=catch_up,
                think=_think(think)[1],
                think_level=None if think_level == "default" else think_level,  # type: ignore[arg-type]
                call_me=call_me,
                prompt_by=who.author,
            )
        except (ValueError, TypeError, KeyError) as exc:
            return ToolResult.failure(str(exc))
        rec = await self._record(who, s.id, None, s, "created")
        return _with_change(
            ToolResult.data(f"Scheduled {s.name!r} ({s.id}); next fire {_local(s.next_fire, s.tz)}."), rec
        )

    @tool(
        "schedule.update",
        description=(
            "Change a scheduled prompt in place - same id, history kept: name, prompt, time (cron or at), "
            "pause/resume (enabled), options. Only the fields you pass change."
        ),
        args=_UpdateArgs,
    )
    async def _update(
        self,
        id: str,
        name: str | None = None,
        prompt: str | None = None,
        cron: str | None = None,
        at: str | None = None,
        tz: str | None = None,
        enabled: bool | None = None,
        catch_up: CatchUp | None = None,
        think: str | None = None,
        think_level: str | None = None,
        call_me: ScheduleCallMe | None = None,
        reason: str = "",
        idempotency_key: str = "",
    ) -> ToolResult:
        before = await self.store.get(id)
        if before is None:
            return ToolResult.failure(f"no schedule {id!r}")
        who = await self._who(idempotency_key)
        fields: dict[str, Any] = {
            "name": name,
            "prompt": prompt,
            "cron": cron,
            "tz": tz,
            "enabled": enabled,
            "catch_up": catch_up,
            "call_me": call_me,
        }
        given, value = _think(think)
        if given:
            fields["think"] = value
        if think_level is not None:
            fields["think_level"] = None if think_level == "default" else think_level
        if prompt is not None and prompt.strip() != before.prompt:
            fields["prompt_by"] = who.author
        try:
            if at:
                fields["at"] = datetime.fromisoformat(at)
            after = await self.store.update(id, **fields)
        except (ValueError, TypeError, KeyError) as exc:
            return ToolResult.failure(f"nothing was changed - {exc}")
        assert after is not None
        rec = await self._record(who, id, before, after, reason)
        if rec is None:
            return ToolResult.empty("Nothing to change: the schedule already has these values.")
        lines = [f"{f.path}: {_short(f.old)} → {_short(f.new)}" for f in rec.fields]
        tail = ""
        if after.prompt_by is not None and before.prompt_by is None:
            tail = "\nThe prompt was written by this run, so it fires asking like a task until Arsen accepts it."
        return _with_change(
            ToolResult.data(
                f"Updated {after.name!r} ({id}); next fire {_local(after.next_fire, after.tz)}.\n"
                + "\n".join(lines)
                + tail
            ),
            rec,
        )

    @tool(
        "schedule.run_now",
        description="Fire a scheduled prompt now, as its Run now button does (a run in its own new chat).",
        args=_IdArgs,
    )
    async def _run_now(self, id: str) -> ToolResult:
        s = await self.store.get(id)
        if s is None:
            return ToolResult.failure(f"no schedule {id!r}")
        if self._scheduler is None:
            return ToolResult.failure("the scheduler is not wired to this tool")
        if await self._own(id):
            return ToolResult.failure("this run IS a fire of that schedule; it cannot fire itself again")
        run_id, conv_id = await self._scheduler.run_now(s)
        return ToolResult.data(f"Fired {s.name!r} now: run {run_id} in conversation {conv_id}.")

    @tool("schedule.delete", description="Delete a scheduled prompt by id.", args=_IdArgs, destructive=True)
    async def _delete(self, id: str, idempotency_key: str = "") -> ToolResult:
        before = await self.store.get(id)
        if before is None:
            return ToolResult.failure(f"no schedule {id!r}")
        await self.store.delete(id)
        rec = await self._record(await self._who(idempotency_key), id, before, None, "deleted")
        return _with_change(ToolResult.data(f"Deleted {before.name!r} ({id})."), rec)


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _with_change(result: ToolResult, rec: ChangeRecord | None) -> ToolResult:
    if rec is None:
        return result
    return result.model_copy(update={"change": rec.model_dump(mode="json")})
