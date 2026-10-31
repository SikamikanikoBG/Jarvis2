"""Delegated tasks over REST (docs/stories/13_delegated_tasks.md): the cards a chat shows, and
what the card's buttons do. The same moves go over the WebSocket (task.cancel, task.steer); these
are for a page that has just loaded and for scripts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from jarvis_core.api.deps import core_of, require_token
from jarvis_proto import Task

router = APIRouter(prefix="/api/tasks", dependencies=[Depends(require_token)])


class SteerBody(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)


class AllowToolBody(BaseModel):
    call_id: str


@router.get("/status")
async def tasks_status(request: Request) -> dict[str, Any]:
    """Whether tasks are on, how many calls are open, and what Settings → Voice should say about a
    call style that could not be brought up to date."""
    core = core_of(request)
    return {
        "enabled": core.settings.tasks.enabled,
        "calls_open": core.tasks.calls_open(),
        "voice_style_note": core.tasks.voice_style_note,
        **await core.tasks.counts(),
    }


@router.get("", response_model=list[Task])
async def list_tasks(request: Request, conversation_id: str | None = None, open: int = 0) -> list[Task]:
    """A chat's tasks (``conversation_id``), or every task; ``open=1`` leaves out the ended ones."""
    return await core_of(request).tasks.store.list(origin=conversation_id, open_only=bool(open))


@router.get("/{task_id}", response_model=Task)
async def get_task(request: Request, task_id: str) -> Task:
    task = await core_of(request).tasks.store.get(task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@router.post("/{task_id}/cancel", response_model=Task)
async def cancel_task(request: Request, task_id: str) -> Task:
    task = await core_of(request).tasks.cancel(task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


calls = APIRouter(prefix="/api/calls", dependencies=[Depends(require_token)])


@calls.post("/{call_id}/end", status_code=204)
async def end_call(request: Request, call_id: str) -> None:
    """Hang-up from a page that is going away (pagehide): the WebSocket may already be closed, a
    keepalive request still goes out. The same exactly-once end as ``call.ended``."""
    await core_of(request).tasks.call_ended(call_id, how="hangup")


@router.post("/{task_id}/allow-tool", response_model=Task)
async def allow_tool(request: Request, task_id: str, body: AllowToolBody) -> Task:
    """ "Always allow this tool in tasks": approve the call the task waits on and add its tool to
    settings.tasks.no_approval. 409 when the task is not waiting on that call or the tool is one
    tasks are never offered."""
    core = core_of(request)
    why = await core.tasks.allow_tool(task_id, body.call_id)
    if why == "no such task":
        raise HTTPException(404, why)
    if why is not None:
        raise HTTPException(409, why)
    task = await core.tasks.store.get(task_id)
    assert task is not None
    return task


@router.post("/{task_id}/steer", response_model=Task)
async def steer_task(request: Request, task_id: str, body: SteerBody) -> Task:
    """His words for the task: into its working run, or a new round when it has stopped."""
    task = await core_of(request).tasks.steer(task_id, body.text)
    if task is None:
        raise HTTPException(404, "no such task")
    return task
