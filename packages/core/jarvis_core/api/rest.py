"""REST: CRUD and read models. Anything that makes the model work goes through the WS/engine."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from jarvis_core import __version__
from jarvis_core.api.deps import core_of, require_token
from jarvis_proto import Conversation, ConversationKind, Message, Run, SearchHit, Settings, ToolSpec
from jarvis_proto.events import ConversationDeleted, ConversationUpdated

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
open_router = APIRouter(prefix="/api")


@open_router.get("/health")
async def health() -> dict[str, Any]:
    """Open on purpose: the UI and Docker use it to tell 'down' from 'wrong token'."""
    return {"ok": True, "version": __version__}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    core = core_of(request)
    specs = core.adapters.all_specs()

    async def probe(role: str, spec: Any) -> dict[str, Any]:
        adapter = core.adapters.for_role(role)  # type: ignore[arg-type]
        res = await adapter.probe()
        return {
            "role": role,
            "provider": spec.provider.value,
            "base_url": spec.base_url,
            "model": spec.model,
            "think": spec.think,
            "ok": res.ok,
            "latency_ms": res.latency_ms,
            "detail": res.detail,
            "models": res.models[:50],
        }

    endpoints = await asyncio.gather(*(probe(r.value, s) for r, s in specs.items()))
    return {
        "version": __version__,
        "endpoints": list(endpoints),
        "tools": core.registry.provider_health(),
        "runs": {"running": len(core.engine.active_run_ids()), "queued": core.engine.queued_count()},
    }


# --- conversations ------------------------------------------------------------------


class ConversationCreate(BaseModel):
    kind: ConversationKind = ConversationKind.CHAT
    title: str = "New chat"


class ConversationPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None
    unread: bool | None = None
    pinned: bool | None = None
    # A persona or standing rule for this chat only; "" clears it.
    instructions: str | None = None


class ForkRequest(BaseModel):
    """Fork BEFORE this message (None = copy the whole transcript)."""

    up_to_message_id: str | None = None


@router.get("/conversations", response_model=list[Conversation])
async def list_conversations(request: Request, archived: int = 0) -> list[Conversation]:
    convs = await core_of(request).store.list_conversations(include_archived=bool(archived))
    return [c for c in convs if bool(archived) == c.archived] if archived else convs


@router.post("/conversations", response_model=Conversation, status_code=201)
async def create_conversation(request: Request, body: ConversationCreate) -> Conversation:
    core = core_of(request)
    conv = await core.store.create_conversation(kind=body.kind, title=body.title)
    core.bus.publish(ConversationUpdated(conversation=conv))
    return conv


@router.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(request: Request, conversation_id: str) -> Conversation:
    conv = await core_of(request).store.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(404, "conversation not found")
    return conv


@router.patch("/conversations/{conversation_id}", response_model=Conversation)
async def patch_conversation(request: Request, conversation_id: str, body: ConversationPatch) -> Conversation:
    core = core_of(request)
    fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump(exclude_none=True).items()}
    if "title" in fields:
        fields["title"] = str(fields["title"]).strip()[:80] or "New chat"
        fields["title_auto"] = 0  # a human named it; the titler leaves it alone from now on
    if "instructions" in fields:
        # Capped, but generously: a real persona is a document, not a sentence — the one V1 kept
        # for the "Massimo Massa" chat is 8,861 characters. It sits in the stable part of the
        # system message, so it is prefilled once and cached from then on; the cap is here to
        # stop a book being pasted in, not to keep it short.
        fields["instructions"] = str(fields["instructions"]).strip()[:16_000]
    conv = await core.store.update_conversation(conversation_id, **fields)
    if conv is None:
        raise HTTPException(404, "conversation not found")
    core.bus.publish(ConversationUpdated(conversation=conv))
    return conv


@router.post("/conversations/{conversation_id}/fork", response_model=Conversation, status_code=201)
async def fork_conversation(request: Request, conversation_id: str, body: ForkRequest) -> Conversation:
    """Copy the transcript up to (not including) a message into a new conversation - how
    "edit and resend" works on an append-only history."""
    core = core_of(request)
    try:
        conv = await core.store.fork_conversation(conversation_id, up_to_message_id=body.up_to_message_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if conv is None:
        raise HTTPException(404, "conversation not found")
    core.bus.publish(ConversationUpdated(conversation=conv))
    return conv


@router.get("/search", response_model=list[SearchHit])
async def search(request: Request, q: str, limit: int = 30) -> list[SearchHit]:
    """Conversations by title, then by message text; one hit per conversation with a snippet."""
    return await core_of(request).store.search(q, limit=min(limit, 100) if limit > 0 else 30)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(request: Request, conversation_id: str) -> Response:
    core = core_of(request)
    for run in await core.store.list_runs(conversation_id):
        if not run.status.terminal:
            await core.engine.cancel(run.id)
    await core.store.delete_conversation(conversation_id)
    core.bus.publish(ConversationDeleted(conversation_id=conversation_id))
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/messages", response_model=list[Message])
async def list_messages(request: Request, conversation_id: str) -> list[Message]:
    core = core_of(request)
    messages = await core.store.list_messages(conversation_id)
    # Attachments live in their own table; the transcript needs them on the message they came with.
    by_message = await core.attachments.for_messages([m.id for m in messages if m.id])
    for m in messages:
        m.attachments = by_message.get(m.id or "", [])
    return messages


@router.get("/conversations/{conversation_id}/runs", response_model=list[Run])
async def list_runs(request: Request, conversation_id: str, limit: int = 50) -> list[Run]:
    # A negative LIMIT means "no limit" to SQLite, so the ceiling has to hold at both ends.
    return await core_of(request).store.list_runs(conversation_id, limit=min(limit, 200) if limit > 0 else 50)


# --- runs --------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_model=Run)
async def get_run(request: Request, run_id: str) -> Run:
    run = await core_of(request).store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@router.get("/runs/{run_id}/events")
async def list_run_events(request: Request, run_id: str, after: int = 0) -> list[dict[str, Any]]:
    return await core_of(request).store.list_events(run_id, after_seq=after)


# --- settings / tools --------------------------------------------------------------


@router.get("/settings", response_model=Settings)
async def get_settings(request: Request) -> Settings:
    return core_of(request).settings


@router.patch("/settings", response_model=Settings)
async def patch_settings(request: Request, body: dict[str, Any]) -> Settings:
    core = core_of(request)
    merged = core.settings.model_dump(mode="json") | body
    try:
        new = Settings.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            422, detail=exc.errors(include_url=False, include_context=False, include_input=False)
        ) from exc
    await core.store.save_settings(new, only_keys=set(body))
    core.apply_settings(new)
    if "mcp_servers" in body:
        await core.reload_tools()
    return new


@router.post("/tools/reload", response_model=list[ToolSpec])
async def reload_tools(request: Request) -> list[ToolSpec]:
    core = core_of(request)
    await core.reload_tools()
    return core.registry.specs()


@router.get("/tools", response_model=list[ToolSpec])
async def list_tools(request: Request) -> list[ToolSpec]:
    return core_of(request).registry.specs()
