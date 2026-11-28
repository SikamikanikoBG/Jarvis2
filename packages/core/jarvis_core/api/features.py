"""REST for the feature modules (docs/API.md): boards, knowledge, skills, schedules, summary."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from jarvis_core.api.deps import agent, core_of, require_token
from jarvis_core.features.admin import Who
from jarvis_core.features.schedules import schedule_diff
from jarvis_proto import (
    Board,
    DeviceLocation,
    Entity,
    EntityDetail,
    Graph,
    LocationView,
    NavView,
    Note,
    Place,
    Project,
    Schedule,
    ScheduleFire,
    Skill,
    Trigger,
    TriggerFire,
)
from jarvis_proto.features import CatchUp, EntityType, NoteColor, ScheduleCallMe
from jarvis_proto.runs import ThinkLevel

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _clamp(value: int, ceiling: int, *, default: int) -> int:
    return min(value, ceiling) if value > 0 else default


# --- boards ------------------------------------------------------------------------------


class BoardCreate(BaseModel):
    name: str


class BoardPatch(BaseModel):
    name: str | None = None
    position: int | None = None


class NoteCreate(BaseModel):
    text: str
    color: NoteColor = "yellow"
    from_message_id: str | None = None


class NotePatch(BaseModel):
    text: str | None = None
    color: NoteColor | None = None
    board_id: str | None = None
    position: int | None = None


@router.get("/boards", response_model=list[Board], openapi_extra=agent("read"))
async def list_boards(request: Request) -> list[Board]:
    return await core_of(request).boards.list_boards()


@router.post("/boards", response_model=Board, status_code=201, openapi_extra=agent("write"))
async def create_board(request: Request, body: BoardCreate) -> Board:
    if not body.name.strip():
        raise HTTPException(422, "name is required")
    return await core_of(request).boards.create_board(body.name)


@router.patch("/boards/{board_id}", response_model=Board, openapi_extra=agent("write"))
async def patch_board(request: Request, board_id: str, body: BoardPatch) -> Board:
    store = core_of(request).boards
    board = await store.update_board(board_id, name=body.name)
    if board is not None and body.position is not None:
        board = await store.reorder_board(board_id, body.position)
    if board is None:
        raise HTTPException(404, "board not found")
    return board


@router.delete("/boards/{board_id}", status_code=204, openapi_extra=agent("write", risk="delete"))
async def delete_board(request: Request, board_id: str) -> Response:
    await core_of(request).boards.delete_board(board_id)
    return Response(status_code=204)


@router.get("/boards/{board_id}/notes", response_model=list[Note], openapi_extra=agent("read"))
async def list_notes(request: Request, board_id: str) -> list[Note]:
    core = core_of(request)
    if await core.boards.get_board(board_id) is None:
        raise HTTPException(404, "board not found")
    return await core.boards.list_notes(board_id)


@router.post("/boards/{board_id}/notes", response_model=Note, status_code=201, openapi_extra=agent("write"))
async def add_note(request: Request, board_id: str, body: NoteCreate) -> Note:
    core = core_of(request)
    if await core.boards.get_board(board_id) is None:
        raise HTTPException(404, "board not found")
    if not body.text.strip():
        raise HTTPException(422, "text is required")
    return await core.boards.add_note(board_id, body.text, color=body.color, from_message_id=body.from_message_id)


@router.patch("/notes/{note_id}", response_model=Note, openapi_extra=agent("write"))
async def patch_note(request: Request, note_id: str, body: NotePatch) -> Note:
    note = await core_of(request).boards.update_note(note_id, **body.model_dump(exclude_none=True))
    if note is None:
        raise HTTPException(404, "note not found")
    return note


@router.delete("/notes/{note_id}", status_code=204, openapi_extra=agent("write", risk="delete"))
async def delete_note(request: Request, note_id: str) -> Response:
    await core_of(request).boards.delete_note(note_id)
    return Response(status_code=204)


# --- knowledge ---------------------------------------------------------------------------


class EntityPatch(BaseModel):
    name: str | None = None
    type: EntityType | None = None
    summary: str | None = None


class MergeBody(BaseModel):
    into: str


@router.get("/kg/entities", response_model=list[Entity], openapi_extra=agent("read"))
async def search_entities(request: Request, q: str = "", limit: int = 50) -> list[Entity]:
    # Clamped at BOTH ends: SQLite reads a negative LIMIT as "no limit", so ?limit=-1 asked for
    # the whole graph. Same for depth below.
    return await core_of(request).knowledge.search(q, limit=_clamp(limit, 200, default=50))


@router.get("/kg/graph", response_model=Graph, openapi_extra=agent("read"))
async def kg_graph(request: Request, center: str | None = None, depth: int = 1, limit: int = 80) -> Graph:
    return await core_of(request).knowledge.graph(
        center=center, depth=_clamp(depth, 5, default=1), limit=_clamp(limit, 300, default=80)
    )


@router.get("/kg/entities/{entity_id}", response_model=EntityDetail, openapi_extra=agent("read"))
async def get_entity(request: Request, entity_id: str) -> EntityDetail:
    detail = await core_of(request).knowledge.detail(entity_id)
    if detail is None:
        raise HTTPException(404, "entity not found")
    return detail


@router.patch("/kg/entities/{entity_id}", response_model=Entity, openapi_extra=agent("write"))
async def patch_entity(request: Request, entity_id: str, body: EntityPatch) -> Entity:
    entity = await core_of(request).knowledge.update(entity_id, name=body.name, type=body.type, summary=body.summary)
    if entity is None:
        raise HTTPException(404, "entity not found")
    return entity


@router.delete("/kg/entities/{entity_id}", status_code=204, openapi_extra=agent("write", risk="delete"))
async def delete_entity(request: Request, entity_id: str) -> Response:
    await core_of(request).knowledge.delete(entity_id)
    return Response(status_code=204)


@router.post("/kg/entities/{entity_id}/merge", response_model=Entity, openapi_extra=agent("write", risk="delete"))
async def merge_entity(request: Request, entity_id: str, body: MergeBody) -> Entity:
    entity = await core_of(request).knowledge.merge(entity_id, body.into)
    if entity is None:
        raise HTTPException(404, "entity not found")
    return entity


# --- skills ------------------------------------------------------------------------------


class SkillContent(BaseModel):
    content: str


class SkillEnabled(BaseModel):
    enabled: bool


@router.get("/skills", response_model=list[Skill], openapi_extra=agent("read"))
async def list_skills(request: Request) -> list[Skill]:
    return await core_of(request).skills.list()


@router.get("/skills/{name}", openapi_extra=agent("read"))
async def get_skill(request: Request, name: str) -> dict[str, str]:
    try:
        content = await core_of(request).skills.get(name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if content is None:
        raise HTTPException(404, "skill not found")
    return {"name": name, "content": content}


@router.put("/skills/{name}", response_model=Skill, openapi_extra=agent("write"))
async def put_skill(request: Request, name: str, body: SkillContent) -> Skill:
    try:
        return await core_of(request).skills.put(name, body.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/skills/{name}", response_model=Skill, openapi_extra=agent("write"))
async def patch_skill(request: Request, name: str, body: SkillEnabled) -> Skill:
    try:
        skill = await core_of(request).skills.set_enabled(name, body.enabled)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if skill is None:
        raise HTTPException(404, "skill not found")
    return skill


@router.delete("/skills/{name}", status_code=204, openapi_extra=agent("write", risk="delete"))
async def delete_skill(request: Request, name: str) -> Response:
    try:
        if not await core_of(request).skills.delete(name):
            raise HTTPException(404, "skill not found")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204)


# --- schedules ---------------------------------------------------------------------------


class ScheduleBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    at: datetime | None = None
    tz: str | None = None
    enabled: bool | None = None
    catch_up: CatchUp | None = None
    think: bool | None = None
    think_level: ThinkLevel | None = None
    call_me: ScheduleCallMe | None = None
    # The project it serves; sent as null on a PATCH it takes the schedule out of its project.
    project_id: str | None = None


@router.get("/schedules", response_model=list[Schedule], openapi_extra=agent("read", prefer="schedule.list"))
async def list_schedules(request: Request) -> list[Schedule]:
    return await core_of(request).schedules.list()


@router.post(
    "/schedules", response_model=Schedule, status_code=201, openapi_extra=agent("none", prefer="schedule.create")
)
async def create_schedule(request: Request, body: ScheduleBody) -> Schedule:
    core = core_of(request)
    if not body.name or not body.prompt:
        raise HTTPException(422, "name and prompt are required")
    try:
        created = await core.schedules.create(
            name=body.name,
            prompt=body.prompt,
            cron=body.cron,
            at=body.at,
            tz=body.tz or core.settings.timezone,
            enabled=True if body.enabled is None else body.enabled,
            catch_up=body.catch_up or "skip",
            think=body.think,
            think_level=body.think_level,
            call_me=body.call_me or "default",
            project_id=body.project_id or None,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await core.admin.changes.record(
        area="schedule", target=created.id, who=Who(), fields=schedule_diff(None, created), reason="created"
    )
    return created


@router.get("/schedules/{schedule_id}", response_model=Schedule, openapi_extra=agent("read", prefer="schedule.get"))
async def get_schedule(request: Request, schedule_id: str) -> Schedule:
    schedule = await core_of(request).schedules.get(schedule_id)
    if schedule is None:
        raise HTTPException(404, "schedule not found")
    return schedule


@router.patch(
    "/schedules/{schedule_id}", response_model=Schedule, openapi_extra=agent("none", prefer="schedule.update")
)
async def patch_schedule(request: Request, schedule_id: str, body: ScheduleBody) -> Schedule:
    """Only the fields sent change. ``think`` / ``think_level`` sent as null go back to the default
    (they could not be cleared before 2026-11-15). A prompt edited here is Arsen's own."""
    core = core_of(request)
    before = await core.schedules.get(schedule_id)
    if before is None:
        raise HTTPException(404, "schedule not found")
    fields = body.model_dump(exclude_none=True)
    for key in ("think", "think_level", "project_id"):
        if key in body.model_fields_set:
            fields[key] = getattr(body, key)
    if body.prompt is not None:
        fields["prompt_by"] = None
    try:
        schedule = await core.schedules.update(schedule_id, **fields)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if schedule is None:
        raise HTTPException(404, "schedule not found")
    if diff := schedule_diff(before, schedule):
        await core.admin.changes.record(area="schedule", target=schedule_id, who=Who(), fields=diff)
    return schedule


@router.delete("/schedules/{schedule_id}", status_code=204, openapi_extra=agent("none", prefer="schedule.delete"))
async def delete_schedule(request: Request, schedule_id: str) -> Response:
    core = core_of(request)
    before = await core.schedules.get(schedule_id)
    if before is None:
        raise HTTPException(404, "schedule not found")
    await core.schedules.delete(schedule_id)
    await core.admin.changes.record(
        area="schedule", target=schedule_id, who=Who(), fields=schedule_diff(before, None), reason="deleted"
    )
    return Response(status_code=204)


@router.post("/schedules/{schedule_id}/accept", response_model=Schedule, openapi_extra=agent("none"))
async def accept_schedule(request: Request, schedule_id: str) -> Schedule:
    """Arsen accepts a prompt a run wrote: it fires with a schedule's own authority from now on
    (until then it asked like a task; docs/stories/14_agent_admin.md)."""
    core = core_of(request)
    before = await core.schedules.get(schedule_id)
    if before is None:
        raise HTTPException(404, "schedule not found")
    schedule = await core.schedules.accept(schedule_id)
    assert schedule is not None
    if diff := schedule_diff(before, schedule):
        await core.admin.changes.record(area="schedule", target=schedule_id, who=Who(), fields=diff, reason="accepted")
    return schedule


@router.post("/schedules/{schedule_id}/run", openapi_extra=agent("none", prefer="schedule.run_now"))
async def run_schedule(request: Request, schedule_id: str) -> dict[str, str]:
    core = core_of(request)
    schedule = await core.schedules.get(schedule_id)
    if schedule is None:
        raise HTTPException(404, "schedule not found")
    run_id, conversation_id = await core.scheduler.run_now(schedule)
    return {"run_id": run_id, "conversation_id": conversation_id}


@router.get(
    "/schedules/{schedule_id}/fires",
    response_model=list[ScheduleFire],
    openapi_extra=agent("read", prefer="schedule.fires"),
)
async def schedule_fires(request: Request, schedule_id: str, limit: int = 20) -> list[ScheduleFire]:
    return await core_of(request).schedules.fires(schedule_id, limit=_clamp(limit, 200, default=20))


# --- projects: schedules that serve one goal (features/projects.py) ---------------------


class ProjectBody(BaseModel):
    name: str | None = None
    goal: str | None = None
    brief: str | None = None
    ends_at: datetime | None = None
    position: int | None = None


@router.get("/projects", response_model=list[Project], openapi_extra=agent("read", prefer="project.list"))
async def list_projects(request: Request) -> list[Project]:
    return await core_of(request).projects.list()


@router.post("/projects", response_model=Project, status_code=201, openapi_extra=agent("none", prefer="project.create"))
async def create_project(request: Request, body: ProjectBody) -> Project:
    core = core_of(request)
    try:
        return await core.projects.create(
            name=body.name or "",
            goal=body.goal or "",
            brief=body.brief or "",
            ends_at=body.ends_at,
            tz=core.settings.timezone,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/projects/{project_id}", response_model=Project, openapi_extra=agent("none", prefer="project.update"))
async def patch_project(request: Request, project_id: str, body: ProjectBody) -> Project:
    """Only the fields sent change; ``ends_at`` sent as null makes it open-ended again."""
    core = core_of(request)
    fields: dict[str, Any] = body.model_dump(exclude_none=True)
    if "ends_at" in body.model_fields_set:
        fields["ends_at"] = body.ends_at
    try:
        project = await core.projects.update(project_id, tz=core.settings.timezone, **fields)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if project is None:
        raise HTTPException(404, "project not found")
    return project


@router.delete("/projects/{project_id}", status_code=204, openapi_extra=agent("none", prefer="project.delete"))
async def delete_project(request: Request, project_id: str) -> Response:
    """The project goes; its schedules stay, on their own."""
    core = core_of(request)
    if await core.projects.get(project_id) is None:
        raise HTTPException(404, "project not found")
    await core.projects.delete(project_id)
    return Response(status_code=204)


# --- location: where Arsen is (features/location.py) -------------------------------------


@router.get("/location", response_model=LocationView, openapi_extra=agent("read", prefer="location.get"))
async def get_location(request: Request) -> LocationView:
    return await core_of(request).location.view()


@router.post("/location", openapi_extra=agent("none"))
async def post_location(request: Request, fix: DeviceLocation) -> dict[str, bool]:
    """A fix from one of his devices while it is open (the phone sends one when it moves, between
    messages). ``kept`` is False when location is off or the fix is older than the one held."""
    return {"kept": await core_of(request).location.update(fix)}


@router.delete("/location", status_code=204, openapi_extra=agent("none"))
async def forget_location(request: Request) -> Response:
    """Forget the last fix (Settings > Location > Forget)."""
    await core_of(request).location.store.forget()
    return Response(status_code=204)


@router.post("/location/owntracks", openapi_extra=agent("none"))
async def owntracks(request: Request) -> list[Any]:
    """OwnTracks in HTTP mode, for a position in the background when the app is closed. Its URL is
    this route with ?token=...; what it sends besides positions is acknowledged and dropped."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    from jarvis_core.features.location import owntracks_fix

    fix = owntracks_fix(body) if isinstance(body, dict) else None
    if fix is not None:
        await core_of(request).location.update(fix)
    return []  # OwnTracks reads the reply as a list of messages for it: none


class PlaceBody(BaseModel):
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_m: float | None = None


@router.get("/places", response_model=list[Place], openapi_extra=agent("read", prefer="location.places"))
async def list_places(request: Request) -> list[Place]:
    return await core_of(request).location.store.places()


@router.post(
    "/places", response_model=Place, status_code=201, openapi_extra=agent("none", prefer="location.place_save")
)
async def create_place(request: Request, body: PlaceBody) -> Place:
    """Without lat/lon: where he is now."""
    service = core_of(request).location
    lat, lon = body.lat, body.lon
    if lat is None or lon is None:
        held = await service.store.last()
        if held is None:
            raise HTTPException(422, "no location yet: give lat and lon")
        lat, lon = held[0].lat, held[0].lon
    try:
        return await service.store.add_place(body.name or "", lat, lon, body.radius_m or 150)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/places/{place_id}", response_model=Place, openapi_extra=agent("none"))
async def patch_place(request: Request, place_id: str, body: PlaceBody) -> Place:
    try:
        place = await core_of(request).location.store.update_place(place_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if place is None:
        raise HTTPException(404, "place not found")
    return place


@router.delete("/places/{place_id}", status_code=204, openapi_extra=agent("none", prefer="location.place_delete"))
async def delete_place(request: Request, place_id: str) -> Response:
    if not await core_of(request).location.store.delete_place(place_id):
        raise HTTPException(404, "place not found")
    return Response(status_code=204)


# --- navigation (features/navigation.py) --------------------------------------------------


@router.get("/nav", response_model=NavView | None, openapi_extra=agent("read", prefer="nav.status"))
async def get_nav(request: Request) -> NavView | None:
    core = core_of(request)
    held = await core.location.store.last()
    return core.navigation.view(held[0] if held else None)


@router.post("/nav/stop", openapi_extra=agent("none", prefer="nav.stop"))
async def stop_nav(request: Request) -> dict[str, bool]:
    """The Stop button on the phone's guidance banner."""
    return {"stopped": await core_of(request).navigation.stop("stopped on the phone")}


# --- triggers (features/triggers.py) ------------------------------------------------------


class TriggerBody(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    project_id: str | None = None


@router.get("/triggers", response_model=list[Trigger], openapi_extra=agent("read", prefer="trigger.list"))
async def list_triggers(request: Request) -> list[Trigger]:
    return await core_of(request).triggers.list()


@router.patch("/triggers/{trigger_id}", response_model=Trigger, openapi_extra=agent("none", prefer="trigger.update"))
async def patch_trigger(request: Request, trigger_id: str, body: TriggerBody) -> Trigger:
    engine = core_of(request).triggers
    if await engine.get(trigger_id) is None:
        raise HTTPException(404, "trigger not found")
    fields = body.model_dump(exclude_none=True)
    if "project_id" in body.model_fields_set:
        fields["project_id"] = body.project_id
    if fields:
        await engine.store.update(trigger_id, fields)
        engine.changed()
    t = await engine.get(trigger_id)
    assert t is not None
    return t


@router.post("/triggers/{trigger_id}/accept", response_model=Trigger, openapi_extra=agent("none"))
async def accept_trigger(request: Request, trigger_id: str) -> Trigger:
    """Arsen accepts a trigger a run made with nobody watching: from now on it may fire."""
    engine = core_of(request).triggers
    if await engine.get(trigger_id) is None:
        raise HTTPException(404, "trigger not found")
    await engine.accept(trigger_id)
    t = await engine.get(trigger_id)
    assert t is not None
    return t


@router.delete("/triggers/{trigger_id}", status_code=204, openapi_extra=agent("none", prefer="trigger.delete"))
async def delete_trigger(request: Request, trigger_id: str) -> Response:
    engine = core_of(request).triggers
    if await engine.get(trigger_id) is None:
        raise HTTPException(404, "trigger not found")
    await engine.store.delete(trigger_id)
    engine.changed()
    return Response(status_code=204)


@router.get(
    "/triggers/{trigger_id}/fires", response_model=list[TriggerFire], openapi_extra=agent("read", prefer="trigger.get")
)
async def trigger_fires(request: Request, trigger_id: str, limit: int = 20) -> list[TriggerFire]:
    return await core_of(request).triggers.store.fires(trigger_id, limit=_clamp(limit, 200, default=20))


# --- Garmin (features/garmin.py) ----------------------------------------------------------


@router.get("/garmin", openapi_extra=agent("read", prefer="garmin.day"))
async def garmin_status(request: Request) -> dict[str, Any]:
    g = core_of(request).garmin
    days = await g.days()
    return {
        "configured": g.configured(),
        "last_sync": g.last_sync.isoformat() if g.last_sync else None,
        "last_error": g.last_error,
        "days_stored": len(days),
        "first_day": days[0] if days else None,
        "today": await g.describe(),
    }


@router.post("/garmin/sync", openapi_extra=agent("none", prefer="garmin.sync"))
async def garmin_sync(request: Request) -> dict[str, Any]:
    from jarvis_core.features.garmin import GarminError

    try:
        return await core_of(request).garmin.sync()
    except GarminError as exc:
        raise HTTPException(422, str(exc)) from exc


# Webhooks: no bearer token - the secret in the path is the key, and an unknown id or a wrong
# secret answer the same 404. Bodies are kept to the first 8,000 characters.
hooks_router = APIRouter()


@hooks_router.post("/hooks/{trigger_id}/{secret}")
async def call_webhook(request: Request, trigger_id: str, secret: str) -> Response:
    raw = (await request.body())[:64_000].decode("utf-8", errors="replace")
    status, out = await core_of(request).triggers.webhook(
        trigger_id, secret, raw, request.headers.get("x-jarvis-chain")
    )
    return Response(content=json.dumps(out), status_code=status, media_type="application/json")


# --- conversation summary (compaction divider) ------------------------------------------


@router.get("/conversations/{conversation_id}/summary", openapi_extra=agent("read"))
async def conversation_summary(request: Request, conversation_id: str) -> dict[str, Any] | None:
    latest = await core_of(request).compactor.latest(conversation_id)
    if latest is None:
        return None
    up_to, text = latest
    return {"up_to_message_id": up_to, "text": text}
