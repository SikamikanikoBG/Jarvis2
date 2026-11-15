"""REST for the feature modules (docs/API.md): boards, knowledge, skills, schedules, summary."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from jarvis_core.api.deps import agent, core_of, require_token
from jarvis_core.features.admin import Who
from jarvis_core.features.schedules import schedule_diff
from jarvis_proto import Board, Entity, EntityDetail, Graph, Note, Schedule, ScheduleFire, Skill
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
    for key in ("think", "think_level"):
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


# --- conversation summary (compaction divider) ------------------------------------------


@router.get("/conversations/{conversation_id}/summary", openapi_extra=agent("read"))
async def conversation_summary(request: Request, conversation_id: str) -> dict[str, Any] | None:
    latest = await core_of(request).compactor.latest(conversation_id)
    if latest is None:
        return None
    up_to, text = latest
    return {"up_to_message_id": up_to, "text": text}
