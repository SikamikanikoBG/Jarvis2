"""Push notifications: the one channel that reaches Arsen when the app is not open.

A scheduled reminder that lands only in the Scheduled folder is invisible until he opens
the app - V1 solved this with a Discord webhook and V2 keeps exactly that. The tool is
mutating (a message goes out) but not destructive (nothing is lost if it is sent twice), so
unattended runs use it freely and interactive runs are not interrupted for it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
from pydantic import BaseModel, Field

from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Settings, ToolResult

log = logging.getLogger(__name__)

_MAX_DISCORD = 1900  # Discord's limit is 2000; leave room for the prefix


class _DiscordFile(BaseModel):
    filename: str = Field(description="e.g. 'news_2026-09-28.html' or 'report.md' - the extension picks the preview.")
    content: str = Field(description="The whole file as text (HTML, Markdown, CSV, ...).")


class _DiscordArgs(BaseModel):
    text: str = Field(description="The message. Plain text or Discord markdown; long text is split.")
    files: list[_DiscordFile] = Field(
        default_factory=list,
        description="Text files to attach (max 10, 8 MB in all) - the full HTML/Markdown document when the "
        "message is its summary, e.g. a digest that could not be saved as an Outlook draft.",
    )


_MAX_FILES = 10
_MAX_FILE_BYTES = 8 * 1024 * 1024


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
            "Keep the text short and well formatted (Discord markdown: **bold**, bullet lists, links); put a "
            "long report or an HTML/Markdown document in `files` so he gets it as an attachment."
        ),
        args=_DiscordArgs,
    )
    async def _discord(self, text: str, files: list[dict[str, str]] | None = None) -> ToolResult:
        url = (self._settings().discord_webhook_url or "").strip()
        if not url.startswith("https://"):
            return ToolResult.failure("no Discord webhook configured (settings.discord_webhook_url)")
        attachments = [(f["filename"], f["content"].encode("utf-8")) for f in files or []][:_MAX_FILES]
        if sum(len(b) for _, b in attachments) > _MAX_FILE_BYTES:
            return ToolResult.failure(
                f"attachments exceed {_MAX_FILE_BYTES // 1_048_576} MB; send fewer or smaller files"
            )
        chunks = _split(text.strip(), _MAX_DISCORD) or [""]
        sent = 0
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            try:
                if last and attachments:
                    # The files ride on the last part, so they arrive under the end of the message.
                    resp = await self._client.post(
                        url,
                        data={"payload_json": json.dumps({"content": chunk})},
                        files=[(f"files[{n}]", (name, body)) for n, (name, body) in enumerate(attachments)],
                    )
                else:
                    resp = await self._client.post(url, json={"content": chunk})
            except httpx.HTTPError as exc:
                return ToolResult.failure(f"Discord unreachable after {sent} part(s): {type(exc).__name__}: {exc}")
            if resp.status_code >= 400:
                return ToolResult.failure(f"Discord HTTP {resp.status_code} after {sent} part(s): {resp.text[:200]}")
            sent += 1
        extra = f", {len(attachments)} file{'s' if len(attachments) != 1 else ''} attached" if attachments else ""
        return ToolResult.data(f"Sent to Discord ({sent} message{'s' if sent != 1 else ''}, {len(text)} chars{extra}).")


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
