"""Projects: several scheduled prompts that serve one bigger goal.

Arsen, 2026-11-26: the schedules kept multiplying, and many of them are separate pieces of one
larger job - the FX nightly analysis, its hourly management pass and its morning audit; three
property briefs; a repo's discussions lead and its star hunt. Each fired knowing only its own
prompt. A project names the goal, holds a brief every member is given, and may end on a date
(its schedules then pause themselves - a trip's watches no longer need a one-off that deletes
them).

When a member fires, its run is told (ProjectBlock): the project, its goal and brief, and every
other member with its timing, how its last run ended and the first lines of what it reported. The
grouping is data Arsen and Jarvis edit, never a list in code.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Project, Run, RunKind, Schedule, ToolResult, new_id
from jarvis_proto.events import ScheduleChanged

if TYPE_CHECKING:
    from jarvis_core.app import Core
    from jarvis_core.db.store import Store
    from jarvis_core.features.schedules import ScheduleStore

NAME_MAX = 120
GOAL_MAX = 2_000
BRIEF_MAX = 8_000
# What a member's run is shown of each sibling's last report: enough to know what it decided.
REPORT_CHARS = 400


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _utc(value: datetime | None, tz: str) -> datetime | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=ZoneInfo(tz))).astimezone(UTC)


class ProjectStore:
    def __init__(self, db: Database, bus: EventBus | None = None) -> None:
        self.db = db
        self.bus = bus

    def _changed(self) -> None:
        # The Schedules screen and the sidebar both reload on a schedule change; a project is
        # part of what they show, so it rides the same event.
        if self.bus is not None:
            self.bus.publish(ScheduleChanged(schedule_id=None))

    async def _members(self) -> dict[str, list[str]]:
        rows = await self.db.fetchall(
            "SELECT id, project_id FROM schedules WHERE project_id IS NOT NULL ORDER BY created_at"
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["project_id"], []).append(r["id"])
        return out

    @staticmethod
    def _row(row: Any, members: list[str]) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            goal=row["goal"],
            brief=row["brief"],
            ends_at=_dt(row["ends_at"]),
            position=row["position"],
            schedule_ids=members,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def list(self) -> list[Project]:
        rows = await self.db.fetchall("SELECT * FROM projects ORDER BY position, created_at")
        members = await self._members()
        return [self._row(r, members.get(r["id"], [])) for r in rows]

    async def get(self, project_id: str) -> Project | None:
        row = await self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            return None
        return self._row(row, (await self._members()).get(project_id, []))

    @staticmethod
    def _check(name: str, goal: str, brief: str) -> None:
        if not name.strip():
            raise ValueError("a project needs a name")
        if len(name) > NAME_MAX:
            raise ValueError(f"the name is {len(name)} characters; at most {NAME_MAX}")
        if len(goal) > GOAL_MAX:
            raise ValueError(f"the goal is {len(goal)} characters; at most {GOAL_MAX}")
        if len(brief) > BRIEF_MAX:
            raise ValueError(f"the brief is {len(brief)} characters; at most {BRIEF_MAX}")

    async def create(
        self, *, name: str, goal: str = "", brief: str = "", ends_at: datetime | None = None, tz: str = "UTC"
    ) -> Project:
        self._check(name, goal, brief)
        row = await self.db.fetchone("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM projects")
        now = _now().isoformat()
        pid = new_id("prj")
        ends = _utc(ends_at, tz)
        await self.db.execute(
            "INSERT INTO projects(id, name, goal, brief, ends_at, position, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                pid,
                name.strip(),
                goal.strip(),
                brief.strip(),
                ends.isoformat() if ends else None,
                row["p"] if row else 0,
                now,
                now,
            ),
        )
        self._changed()
        project = await self.get(pid)
        assert project is not None
        return project

    async def update(self, project_id: str, *, tz: str = "UTC", **fields: Any) -> Project | None:
        """Only the fields given change; ``ends_at`` given as None makes it open-ended again."""
        current = await self.get(project_id)
        if current is None:
            return None
        data = current.model_dump()
        for key in ("name", "goal", "brief", "position"):
            if fields.get(key) is not None:
                data[key] = fields[key]
        if "ends_at" in fields:
            data["ends_at"] = _utc(fields["ends_at"], tz)
        self._check(data["name"], data["goal"], data["brief"])
        await self.db.execute(
            "UPDATE projects SET name=?, goal=?, brief=?, ends_at=?, position=?, updated_at=? WHERE id=?",
            (
                str(data["name"]).strip(),
                str(data["goal"]).strip(),
                str(data["brief"]).strip(),
                data["ends_at"].isoformat() if data["ends_at"] else None,
                int(data["position"]),
                _now().isoformat(),
                project_id,
            ),
        )
        self._changed()
        return await self.get(project_id)

    async def delete(self, project_id: str) -> None:
        """The project goes; its schedules stay, on their own."""
        await self.db.execute("UPDATE schedules SET project_id = NULL WHERE project_id = ?", (project_id,))
        await self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._changed()

    async def last_report(self, conversation_id: str | None) -> str | None:
        """The last thing a fire's run said in its chat - what Arsen read as its report."""
        if not conversation_id:
            return None
        row = await self.db.fetchone(
            "SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant' AND partial = 0"
            " AND TRIM(content) != '' ORDER BY created_at DESC, id DESC LIMIT 1",
            (conversation_id,),
        )
        return row["content"] if row else None


def _when(s: Schedule) -> str:
    return s.cron or (f"once at {s.at.isoformat()}" if s.at else "?")


def _local(value: datetime | None, tz: str) -> str:
    if value is None:
        return "never"
    return value.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def _gist(text: str, limit: int = REPORT_CHARS) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def project_lines(
    project: Project, schedules: list[Schedule], projects: ProjectStore, *, skip: str | None = None, tz: str
) -> list[str]:
    """One line per member: timing, state, the last run's outcome and the gist of its report."""
    lines: list[str] = []
    for s in schedules:
        if s.project_id != project.id or s.id == skip:
            continue
        state = "on" if s.enabled else "paused"
        line = f"- [{s.id}] {s.name} — {_when(s)} ({state})"
        if s.enabled and s.next_fire:
            line += f"; next {_local(s.next_fire, tz)}"
        # The newest fire, a "Run now" included (last_fired_for counts only the scheduled slots).
        fire = await projects.db.fetchone(
            "SELECT f.scheduled_for, f.conversation_id, r.status FROM schedule_fires f"
            " LEFT JOIN runs r ON r.id = f.run_id WHERE f.schedule_id = ? ORDER BY f.scheduled_for DESC LIMIT 1",
            (s.id,),
        )
        if fire is not None:
            line += f"; last fired {_local(_dt(fire['scheduled_for']), tz)}: {fire['status'] or 'not started'}"
            report = await projects.last_report(fire["conversation_id"])
            if report:
                line += f'\n  reported: "{_gist(report)}"'
        lines.append(line)
    return lines


class ProjectBlock:
    """A scheduled run whose schedule belongs to a project is told what the project is for and
    what its other schedules are doing - so the hourly FX pass knows what last night's analysis
    decided, and the audit knows what the pass closed. Per-run context, only for members."""

    def __init__(self, projects: ProjectStore, schedules: ScheduleStore, store: Store, tz: Callable[[], str]) -> None:
        self._projects = projects
        self._schedules = schedules
        self._store = store
        self._tz = tz

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if run.kind is not RunKind.SCHEDULED:
            return None
        conv = await self._store.get_conversation(run.conversation_id)
        schedule = await self._schedules.get(conv.folder_key) if conv and conv.folder_key else None
        if schedule is None or not schedule.project_id:
            return None
        project = await self._projects.get(schedule.project_id)
        if project is None:
            return None
        tz = schedule.tz or self._tz()
        parts = [
            f"## Project: {project.name}\nThis scheduled prompt is one part of the project '{project.name}'"
            " - several schedules that serve one goal. Work as one of them: build on what the others found"
            " and decided instead of starting from nothing, and do not redo their part."
        ]
        if project.goal:
            parts.append(f"Goal: {project.goal}")
        if project.brief:
            parts.append(f"Brief for every schedule in this project:\n{project.brief}")
        if project.ends_at:
            parts.append(f"The project ends {_local(project.ends_at, tz)}; its schedules pause then.")
        siblings = await project_lines(project, await self._schedules.list(), self._projects, skip=schedule.id, tz=tz)
        if siblings:
            parts.append(
                "The project's other schedules (schedule.get / schedule.fires and the conversation they"
                " name give a report in full):\n" + "\n".join(siblings)
            )
        return "\n\n".join(parts)


# --- tools ------------------------------------------------------------------------------


class _IdArgs(BaseModel):
    id: str


class _CreateArgs(BaseModel):
    name: str = Field(description="Short name, e.g. 'FX trading'")
    goal: str = Field(default="", description="One or two sentences: what the schedules in it achieve together")
    brief: str = Field(
        default="",
        description="Standing instructions every schedule in it is given when it fires (paths, playbooks, delivery rules)",
    )
    ends_at: str | None = Field(
        default=None, description="ISO datetime after which its schedules pause; none = open-ended"
    )
    schedule_ids: list[str] = Field(default_factory=list, description="Schedules to put in it right away")


class _UpdateArgs(BaseModel):
    id: str
    name: str | None = None
    goal: str | None = None
    brief: str | None = None
    ends_at: str | None = Field(default=None, description="ISO datetime; '' = open-ended again")
    add: list[str] = Field(default_factory=list, description="Schedule ids to move into this project")
    remove: list[str] = Field(default_factory=list, description="Schedule ids to take out (they stay, on their own)")


class ProjectTools(BuiltinProvider):
    """``project.*``: group the scheduled prompts that serve one goal.

    A goal or brief is standing instruction to every member's run, so only Arsen writes them - in
    the UI or by asking Jarvis in his own chat; a run nobody is watching may read projects and
    file schedules into them, not rewrite what they tell the others."""

    name = "project"

    def __init__(
        self, projects: ProjectStore, schedules: ScheduleStore, *, tz: Callable[[], str], core: Core | None = None
    ) -> None:
        self.projects = projects
        self.schedules = schedules
        self._tz = tz
        self._core = core
        super().__init__()

    async def _by_run(self) -> bool:
        if self._core is None:
            return False
        from jarvis_core.features.admin import who_now

        return (await who_now(self._core)).author is not None

    async def _move(self, ids: list[str], project_id: str | None) -> list[str]:
        """Move schedules through the schedule tools' own path so the trail records each one."""
        from jarvis_core.features.admin import who_now
        from jarvis_core.features.schedules import schedule_diff

        done: list[str] = []
        for sid in ids:
            before = await self.schedules.get(sid)
            if before is None:
                raise ValueError(f"no schedule {sid!r}")
            after = await self.schedules.update(sid, project_id=project_id)
            assert after is not None
            if self._core is not None and (fields := schedule_diff(before, after)):
                await self._core.admin.changes.record(
                    area="schedule", target=sid, who=await who_now(self._core), fields=fields, reason="project"
                )
            done.append(after.name)
        return done

    @tool(
        "project.list",
        description="List projects - groups of schedules that serve one goal - with their schedules.",
        read_only=True,
        idempotent=True,
    )
    async def _list(self) -> ToolResult:
        projects = await self.projects.list()
        schedules = await self.schedules.list()
        loose = [s for s in schedules if not s.project_id]
        if not projects:
            return ToolResult.empty(f"No projects yet; {len(loose)} schedule(s) stand on their own.")
        names = {s.id: s for s in schedules}
        lines: list[str] = []
        for p in projects:
            on = sum(1 for sid in p.schedule_ids if names.get(sid) and names[sid].enabled)
            head = f"- [{p.id}] {p.name} — {on}/{len(p.schedule_ids)} on"
            if p.ends_at:
                head += f", ends {_local(p.ends_at, self._tz())}"
            lines.append(head + (f"\n  goal: {_gist(p.goal, 200)}" if p.goal else ""))
            lines += [f"  · [{sid}] {names[sid].name}" for sid in p.schedule_ids if sid in names]
        if loose:
            lines.append(
                f"- on their own: {len(loose)} schedule(s) - " + ", ".join(f"[{s.id}] {s.name}" for s in loose)
            )
        return ToolResult.data("\n".join(lines), count=len(projects))

    @tool(
        "project.get",
        description="One project in full: goal, brief, end, and each schedule's timing, last outcome and the gist of its last report.",
        args=_IdArgs,
        read_only=True,
        idempotent=True,
    )
    async def _get(self, id: str) -> ToolResult:
        p = await self.projects.get(id)
        if p is None:
            return ToolResult.failure(f"no project {id!r}")
        tz = self._tz()
        lines = [f"[{p.id}] {p.name}"]
        if p.goal:
            lines.append(f"goal: {p.goal}")
        if p.ends_at:
            lines.append(f"ends: {_local(p.ends_at, tz)}")
        if p.brief:
            lines.append(f"brief:\n{p.brief}")
        members = await project_lines(p, await self.schedules.list(), self.projects, tz=tz)
        lines.append("schedules:\n" + ("\n".join(members) if members else "(none yet)"))
        return ToolResult.data("\n".join(lines))

    @tool(
        "project.create",
        description=(
            "Create a project: a named goal that several schedules serve together, optionally with a brief "
            "they are all given and an end date. Put schedules in it with schedule_ids."
        ),
        args=_CreateArgs,
    )
    async def _create(
        self,
        name: str,
        goal: str = "",
        brief: str = "",
        ends_at: str | None = None,
        schedule_ids: list[str] | None = None,
    ) -> ToolResult:
        if (goal or brief) and await self._by_run():
            return ToolResult.failure(
                "only Arsen writes a project's goal and brief; create it with a name, he fills them in"
            )
        try:
            p = await self.projects.create(
                name=name,
                goal=goal,
                brief=brief,
                ends_at=datetime.fromisoformat(ends_at) if ends_at else None,
                tz=self._tz(),
            )
            moved = await self._move(schedule_ids or [], p.id)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.data(
            f"Created project {p.name!r} ({p.id})" + (f" with {', '.join(moved)}." if moved else ".")
        )

    @tool(
        "project.update",
        description="Change a project: name, goal, brief, end; move schedules in (add) or out (remove). Only what you pass changes.",
        args=_UpdateArgs,
    )
    async def _update(
        self,
        id: str,
        name: str | None = None,
        goal: str | None = None,
        brief: str | None = None,
        ends_at: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> ToolResult:
        if await self.projects.get(id) is None:
            return ToolResult.failure(f"no project {id!r}")
        if (goal is not None or brief is not None) and await self._by_run():
            return ToolResult.failure("only Arsen writes a project's goal and brief; tell him what you would change")
        fields: dict[str, Any] = {"name": name, "goal": goal, "brief": brief}
        try:
            if ends_at is not None:
                fields["ends_at"] = datetime.fromisoformat(ends_at) if ends_at else None
            p = await self.projects.update(id, tz=self._tz(), **fields)
            assert p is not None
            members = set(p.schedule_ids)
            moved_in = await self._move([s for s in (add or []) if s not in members], id)
            moved_out = await self._move([s for s in (remove or []) if s in members], None)
        except ValueError as exc:
            return ToolResult.failure(f"not everything was changed - {exc}")
        tail = "".join(
            [
                f"\nmoved in: {', '.join(moved_in)}" if moved_in else "",
                f"\ntaken out: {', '.join(moved_out)}" if moved_out else "",
            ]
        )
        return ToolResult.data(f"Updated project {p.name!r} ({id}).{tail}")

    @tool(
        "project.delete",
        description="Delete a project; its schedules stay, on their own.",
        args=_IdArgs,
        destructive=True,
    )
    async def _delete(self, id: str) -> ToolResult:
        p = await self.projects.get(id)
        if p is None:
            return ToolResult.failure(f"no project {id!r}")
        await self.projects.delete(id)
        return ToolResult.data(f"Deleted project {p.name!r}; its {len(p.schedule_ids)} schedule(s) stay on their own.")
