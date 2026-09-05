"""OpenAI-compatible façade: any chat front-end (Open WebUI, scripts) can talk to Jarvis.

``POST /v1/chat/completions`` takes the LAST user message as the input of a new run; the
conversation is the one named by ``X-Jarvis-Conversation`` (or a new ``collab`` conversation
under the caller's key). History lives in Jarvis, not in the request body.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jarvis_core.api.collab import Identity, identify
from jarvis_core.api.deps import core_of
from jarvis_core.engine.bus import Subscriber
from jarvis_proto import ConversationKind, RunKind
from jarvis_proto.events import ConversationUpdated

router = APIRouter(prefix="/v1")


class _Msg(BaseModel):
    role: str
    content: Any = ""


class ChatBody(BaseModel):
    model: str = "jarvis"
    messages: list[_Msg] = Field(default_factory=list)
    stream: bool = False


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content)


@router.get("/models")
async def models(_: Identity = Depends(identify)) -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "jarvis", "object": "model", "owned_by": "jarvis"}]}


@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatBody, who: Identity = Depends(identify)) -> Any:
    core = core_of(request)
    user_text = next((_text_of(m.content) for m in reversed(body.messages) if m.role == "user"), "")
    if not user_text.strip():
        raise HTTPException(422, "no user message")
    conv_id = request.headers.get("x-jarvis-conversation")
    conv = await core.store.get_conversation(conv_id) if conv_id else None
    if conv is None:
        label = who.key.name if who.key else "openai"
        conv = await core.store.create_conversation(
            kind=ConversationKind.COLLAB,
            title=user_text.strip().splitlines()[0][:60],
            folder_key=who.key.id if who.key else "openai",
            folder_label=label,
        )
        core.bus.publish(ConversationUpdated(conversation=conv))
    sub = Subscriber(name="openai")
    sub.conversations.add(conv.id)
    core.bus.attach(sub)
    run, _ = await core.engine.create_run(text=user_text, conversation_id=conv.id, kind=RunKind.COLLAB)
    created = int(time.time())
    cid = f"chatcmpl-{run.id}"
    headers = {"X-Jarvis-Conversation": conv.id, "X-Jarvis-Run": run.id}

    async def events() -> AsyncIterator[Any]:
        try:
            async with asyncio.timeout(run.budget.max_seconds + 30):
                while True:
                    ev = await sub.queue.get()
                    if getattr(ev, "run_id", None) != run.id:
                        continue
                    yield ev
                    if ev.type in {"run.done", "run.failed", "run.cancelled"}:
                        return
        finally:
            core.bus.detach(sub)

    if body.stream:

        async def sse() -> AsyncIterator[bytes]:
            def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
                data = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "jarvis",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

            yield chunk({"role": "assistant", "content": ""})
            async for ev in events():
                if ev.type == "model.delta":
                    yield chunk({"content": ev.text} if ev.kind == "text" else {"reasoning": ev.text})
                elif ev.type == "tool.call":
                    yield chunk({"content": f"\n\n> tool: {ev.name}\n\n"})
                elif ev.type == "run.failed":
                    yield chunk({"content": f"\n\n[run failed] {ev.error}"}, "stop")
                elif ev.type in {"run.done", "run.cancelled"}:
                    yield chunk({}, "stop")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    async for ev in events():
        if ev.type == "model.delta":
            (text_parts if ev.kind == "text" else reasoning_parts).append(ev.text)
        elif ev.type == "model.done":
            usage["prompt_tokens"] += ev.usage.prompt_tokens
            usage["completion_tokens"] += ev.usage.completion_tokens
        elif ev.type == "run.failed":
            text_parts.append(f"\n\n[run failed] {ev.error}")
    # The persisted final message is authoritative (intermediate tool-turn text is not the answer).
    final = await core.store.get_run(run.id)
    reply = "".join(text_parts)
    if final is not None:
        msgs = await core.store.list_run_messages(run.id)
        for m in reversed(msgs):
            if m.role.value == "assistant" and m.content.strip():
                reply = m.content
                break
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": "jarvis",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply,
                        **({"reasoning": "".join(reasoning_parts)} if reasoning_parts else {}),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {**usage, "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"]},
        },
        headers=headers,
    )
