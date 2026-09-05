"""The one WebSocket. Frames are ``ClientMessage`` in, ``ServerEvent`` out."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from jarvis_core.api.deps import core_of_ws, ws_token_ok
from jarvis_core.engine.bus import Subscriber
from jarvis_proto import (
    Pong,
    RunCancelRequest,
    RunCreateRequest,
    Subscribe,
    ToolConfirmRequest,
    Unsubscribe,
    parse_client_message,
)
from jarvis_proto.events import ConversationUpdated, Ping, RunUpdated
from jarvis_proto.runs import RunStatus

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    if not ws_token_ok(ws):
        await ws.close(code=4401, reason="unauthorized")
        return
    core = core_of_ws(ws)
    await ws.accept()
    sub = Subscriber(name=f"ws:{ws.client.host if ws.client else '?'}")
    core.bus.attach(sub)

    async def writer() -> None:
        while True:
            event = await sub.queue.get()
            await ws.send_text(event.model_dump_json())

    writer_task = asyncio.create_task(writer())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = parse_client_message(json.loads(raw))
            except (ValidationError, json.JSONDecodeError) as exc:
                log.debug("bad client frame: %s", exc)
                continue
            if isinstance(msg, Ping):
                sub.deliver(Pong())
            elif isinstance(msg, Subscribe):
                sub.conversations.add(msg.conversation_id)
                for run in await core.store.list_runs(msg.conversation_id, limit=10):
                    if not run.status.terminal and run.status is not RunStatus.INTERRUPTED:
                        sub.deliver(RunUpdated(run=run))
                conv = await core.store.get_conversation(msg.conversation_id)
                if conv is not None and conv.unread:
                    await core.store.update_conversation(conv.id, unread=0)
            elif isinstance(msg, Unsubscribe):
                sub.conversations.discard(msg.conversation_id)
            elif isinstance(msg, RunCreateRequest):
                conversation_id = msg.conversation_id
                if conversation_id is None:
                    # Subscribe before the run exists so run.queued and the user message
                    # reach this client; the engine titles the conversation on first message.
                    conv = await core.store.create_conversation()
                    core.bus.publish(ConversationUpdated(conversation=conv))
                    conversation_id = conv.id
                sub.conversations.add(conversation_id)
                await core.engine.create_run(
                    text=msg.text,
                    conversation_id=conversation_id,
                    kind=msg.kind,
                    think=msg.think,
                    think_level=msg.think_level,
                )
            elif isinstance(msg, RunCancelRequest):
                await core.engine.cancel(msg.run_id)
            elif isinstance(msg, ToolConfirmRequest):
                await core.engine.confirm(msg.run_id, msg.call_id, msg.approved, msg.note)
            if sub.dead:
                await ws.close(code=1008, reason="slow consumer")
                break
    except WebSocketDisconnect:
        pass
    finally:
        core.bus.detach(sub)
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer_task
