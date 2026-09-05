"""Push notifications: the one channel that reaches Arsen when the app is not open.

A scheduled reminder that lands only in the Scheduled folder is invisible until he opens
the app - V1 solved this with a Discord webhook and V2 keeps exactly that. The tool is
mutating (a message goes out) but not destructive (nothing is lost if it is sent twice), so
unattended runs use it freely and interactive runs are not interrupted for it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from pydantic import BaseModel, Field

from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Settings, ToolResult

log = logging.getLogger(__name__)

_MAX_DISCORD = 1900  # Discord's limit is 2000; leave room for the prefix


class _DiscordArgs(BaseModel):
    text: str = Field(description="The message. Plain text or Discord markdown; long text is split.")


class NotifyTools(BuiltinProvider):
    name = "notify"

    def __init__(self, settings: Callable[[], Settings], client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        super().__init__()

    async def aclose(self) -> None:
        await self._client.aclose()

    @tool(
        "notify.discord",
        description=(
            "Send a push notification to Arsen's Discord (his phone buzzes). Use it for time-sensitive "
            "reminders and for the summary of a scheduled run he should see without opening the app. "
            "Keep it short; the full report belongs in the reply."
        ),
        args=_DiscordArgs,
    )
    async def _discord(self, text: str) -> ToolResult:
        url = (self._settings().discord_webhook_url or "").strip()
        if not url.startswith("https://"):
            return ToolResult.failure("no Discord webhook configured (settings.discord_webhook_url)")
        chunks = _split(text.strip(), _MAX_DISCORD) or [""]
        sent = 0
        for chunk in chunks:
            try:
                resp = await self._client.post(url, json={"content": chunk})
            except httpx.HTTPError as exc:
                return ToolResult.failure(f"Discord unreachable after {sent} part(s): {type(exc).__name__}: {exc}")
            if resp.status_code >= 400:
                return ToolResult.failure(f"Discord HTTP {resp.status_code} after {sent} part(s): {resp.text[:200]}")
            sent += 1
        return ToolResult.data(f"Sent to Discord ({sent} message{'s' if sent != 1 else ''}, {len(text)} chars).")


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts
