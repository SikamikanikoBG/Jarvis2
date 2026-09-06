"""Conversation titles the way ChatGPT / claude.ai name a chat: a few words from the classifier
after the first exchange. The first line of the first message stands in until then; a rename by
the user (``title_auto = 0``) is never overwritten.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from jarvis_proto import Message

log = logging.getLogger(__name__)

_PROMPT = """Name this conversation in 3 to 6 words, in the language of the user's message.
No quotes, no trailing period, no "Conversation about". Reply with the title only.

User: {user}

Assistant: {reply}"""


class Titler:
    def __init__(self, adapter: Callable[[], Any]) -> None:
        self._adapter = adapter  # the classifier role's adapter (thinking off, cheap)

    async def title(self, user_text: str, reply_text: str) -> str | None:
        """A short title, or None when the model gave nothing usable (the caller keeps the old one)."""
        from jarvis_core.models.base import ModelTextChunk

        prompt = _PROMPT.format(user=user_text.strip()[:1500], reply=reply_text.strip()[:1500])
        text = ""
        try:
            adapter = self._adapter()
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
        except Exception as exc:
            log.warning("titler skipped: %s", exc)
            return None
        return clean_title(text)


def clean_title(text: str) -> str | None:
    line = next((ln.strip() for ln in text.strip().splitlines() if ln.strip()), "")
    line = line.strip(' "\'`«»“”‘’').rstrip(".").strip()
    if line.lower().startswith(("title:", "заглавие:")):
        line = line.split(":", 1)[1].strip().strip(' "\'')
    if not line or len(line) < 2:
        return None
    return (line[:57] + "…") if len(line) > 60 else line
