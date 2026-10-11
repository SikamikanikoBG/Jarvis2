"""Jarvis calls back (docs/stories/11_call_me.md).

Arsen sends a task with the bell on (or says "call me when you are done"), puts the phone away,
and when the run ends the phone rings: a notification from Jarvis that opens the app on an
incoming call in that chat, where answering reads him the result.

This module decides *when* and *what*: it watches the bus for a run that asked for a ring
(``Run.call_me``) and ends - done, failed - or stops to ask him something. The carrier is
notify's: web push to his devices, Discord with the same link when none of them takes it.

Two runs ending together are one ring, not two: rings are gathered for a few seconds and the
later one rings, saying both.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from jarvis_core.engine.bus import Subscriber
from jarvis_proto import Role
from jarvis_proto.events import RunDone, RunFailed, RunWaitingUser

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

# How long a ring waits for another run that is ending at the same moment.
GATHER_S = 4.0
# What a ring reads aloud: the start of the reply, not all of it - the rest is in the chat.
SAY_SENTENCES = 3
SAY_CHARS = 600
BODY_CHARS = 160


@dataclass(slots=True)
class Ring:
    run_id: str
    conversation_id: str
    kind: str  # "done" | "failed" | "waiting"
    chat: str  # the chat's title
    say: str  # what the answered call reads first


class CallMe:
    def __init__(self, core: Core, *, gather_s: float = GATHER_S) -> None:
        self.core = core
        self.gather_s = gather_s
        self._sub: Subscriber | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: list[Ring] = []
        self._flush: asyncio.Task[None] | None = None
        self._rung: set[tuple[str, str]] = set()  # (run, what): each rings once

    async def start(self) -> None:
        self._sub = Subscriber("callme", everything=True)
        self.core.bus.attach(self._sub)
        self._task = asyncio.create_task(self._consume(self._sub), name="callme-events")

    async def stop(self) -> None:
        if self._sub is not None:
            self.core.bus.detach(self._sub)
            self._sub = None
        for task in (self._task, self._flush):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._flush = None

    async def _consume(self, sub: Subscriber) -> None:
        while True:
            event = await sub.queue.get()
            try:
                await self.on_event(event)
            except Exception:
                log.exception("call-me: %s not handled", getattr(event, "type", type(event).__name__))

    async def on_event(self, event: Any) -> None:
        # (kind, what rings once, the words) - a done run's words are its reply, read below.
        # Each question a run stops on is its own ring; its end is one more.
        said: str | None
        if isinstance(event, RunDone):
            kind, once, said = "done", "done", None
        elif isinstance(event, RunFailed):
            kind, once, said = "failed", "failed", event.error
        elif isinstance(event, RunWaitingUser):
            kind, once, said = "waiting", f"waiting:{event.call_id}", event.reason
        else:
            return
        run = await self.core.store.get_run(event.run_id)
        if run is None or not run.call_me or (run.id, once) in self._rung:
            return
        self._rung.add((run.id, once))
        if len(self._rung) > 1000:
            self._rung.clear()  # a memory of the last thousand rings is plenty against a double ring
        if said is None:
            replies = [m for m in await self.core.store.list_run_messages(run.id) if m.role is Role.ASSISTANT]
            said = next((m.content for m in reversed(replies) if m.content.strip()), "")
        say = plain(said)
        conv = await self.core.store.get_conversation(run.conversation_id)
        self._pending.append(
            Ring(
                run_id=run.id, conversation_id=run.conversation_id, kind=kind, chat=conv.title if conv else "", say=say
            )
        )
        if self._flush is None or self._flush.done():
            self._flush = asyncio.create_task(self._ring_later(), name="callme-ring")

    async def _ring_later(self) -> None:
        await asyncio.sleep(self.gather_s)
        rings, self._pending = self._pending, []
        if rings:
            await self.ring(rings)

    async def ring(self, rings: list[Ring]) -> str | None:
        """One ring for these runs; the last one is where answering it goes."""
        last = rings[-1]
        if len(rings) == 1:
            title = {"done": "Jarvis is done", "failed": "Jarvis: it failed", "waiting": "Jarvis needs you"}[last.kind]
            title = f"{title} · {last.chat}" if last.chat else title
            say = last.say
        else:
            title = f"Jarvis: {len(rings)} tasks ended"
            say = " ".join(f"{r.chat}: {sentences(r.say, 1)}" for r in rings if r.say)
        body = clip(say, BODY_CHARS) or title
        # Answering lands on an incoming call in that chat (a test ring has none: a new chat).
        chat = f"/c/{quote(last.conversation_id)}" if last.conversation_id else "/"
        path = f"{chat}?ring={quote(last.run_id or 'test')}"
        carrier = await self.core.notify.reach(
            title=title,
            body=body,
            link=self.core.notify.link(path),
            payload={
                "type": "call",
                "kind": last.kind,
                "conversation_id": last.conversation_id,
                "run_id": last.run_id,
                "say": clip(say, SAY_CHARS),
                "path": path,
            },
        )
        if carrier is None:
            log.warning("call-me: no device took the ring for %s and Discord is not set up", last.run_id)
        return carrier


_CODE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"(^|\s)[#>|]+\s*|[*_~`]+|^\s*(?:[-•]|\d+[.)])\s+", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def plain(text: str) -> str:
    """Markdown down to words a voice can say and a notification can show."""
    text = _CODE.sub(" ", text or "")
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _MARKS.sub(" ", text)
    return sentences(re.sub(r"\s+", " ", text).strip(), SAY_SENTENCES)


def sentences(text: str, n: int) -> str:
    return " ".join(_SENTENCE_END.split(text)[:n]).strip()


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
