"""files.attach: a file in the chat, for Arsen to open or download on his phone.

Discord was the way a file reached him - a digest, a brief, a report as an attachment to a
webhook post - because the chat could not carry one (2026-10-14: "причината да искам дискорд е
да мога да отворя файлове на мобилния си"). Now the reply can: the run attaches the file, the
end of the run rings his phone, and the file is under the reply with Open and Download.

Two sources. ``content``: a text file written in the run (HTML, Markdown, CSV ...). ``host`` +
``path``: any file on a machine Jarvis works on (a PDF, a workbook, a picture), fetched whole
with the host's internal ``fs_fetch`` - never through the model's context.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import PurePath
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.features.attachments import AttachmentError
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import ToolResult

if TYPE_CHECKING:
    from jarvis_core.app import Core


class _AttachArgs(BaseModel):
    filename: str = Field(
        default="",
        description="The name he will see and save it under, e.g. 'realty_brief_2026-10-14.html'. "
        "With a path it defaults to the file's own name.",
    )
    content: str | None = Field(default=None, description="The whole file as text, when you wrote it in this run.")
    host: str | None = Field(default=None, description="The host the file is on (e.g. 'jarvisvm', 'workspace').")
    path: str | None = Field(default=None, description="The file's full path on that host. Any type, up to 25 MB.")


class FilesTools(BuiltinProvider):
    name = "files"

    def __init__(self, core: Core) -> None:
        self._core = core
        super().__init__()

    @tool(
        "files.attach",
        description=(
            "Attach a file to your reply in this chat, so Arsen can open or download it on his phone. Use it "
            "whenever the result IS a file - a digest, a brief, a report, a document you made or found on a host "
            "- instead of sending it to Discord. Give `content` for a text file you wrote, or `host` + `path` for "
            "any file on a host (PDF, Excel, pictures). The file appears under your reply; say in one line what it is."
        ),
        args=_AttachArgs,
    )
    async def _attach(
        self, filename: str = "", content: str | None = None, host: str | None = None, path: str | None = None
    ) -> ToolResult:
        run_id = current_run_id.get()
        conversation_id = current_conversation_id.get()
        if not run_id or not conversation_id:
            return ToolResult.failure("files can only be attached inside a chat's run")
        mime: str | None = None
        if content is not None:
            data = content.encode("utf-8")
            if not filename:
                return ToolResult.failure("give a filename for the content (e.g. 'report.html')")
        elif host and path:
            fetched = await self._fetch(host, path)
            if isinstance(fetched, ToolResult):
                return fetched
            data, mime, own = fetched
            filename = filename or own
        else:
            return ToolResult.failure("give either content (with a filename) or host + path")
        try:
            att = await self._core.attachments.add_outgoing(
                data=data, filename=filename, mime=mime, conversation_id=conversation_id, run_id=run_id
            )
        except AttachmentError as exc:
            return ToolResult.failure(str(exc))
        size = f"{att.bytes // 1024} kB" if att.bytes >= 1024 else f"{att.bytes} B"
        return ToolResult.data(f"Attached {att.name} ({size}); it appears under your reply, with Open and Download.")

    async def _fetch(self, host: str, path: str) -> tuple[bytes, str | None, str] | ToolResult:
        fetched = await fetch_host_file(self._core, host, path)
        return ToolResult.failure(fetched) if isinstance(fetched, str) else fetched


async def fetch_host_file(core: Core, host: str, path: str) -> tuple[bytes, str | None, str] | str:
    """The file from the host: whole, as bytes, via its internal fs_fetch, or why not. A host that
    has none yet (an older build) still gives a text file through fs_read. files.attach and
    browser.upload's host + path both come through here."""
    registry = core.registry
    name = PurePath(path.replace("\\", "/")).name or "file"
    if registry.get(f"{host}.fs_fetch") is not None:
        res = await registry.call(
            f"{host}.fs_fetch",
            {"path": path},
            cancel=asyncio.Event(),
            idempotency_key=f"attach:{current_run_id.get()}:{host}:{path}",
            timeout_s=120,
        )
        if res.kind.value == "error":
            return f"{host} could not hand over {path}: {res.text[:300]}"
        try:
            payload = json.loads(res.text)
            return base64.b64decode(payload["base64"]), payload.get("mime"), payload.get("name") or name
        except (ValueError, KeyError, TypeError) as exc:
            return f"{host} answered fs_fetch with something unreadable: {exc}"
    if registry.get(f"{host}.fs_read") is None:
        return f"no host named {host!r} with file tools is connected"
    res = await registry.call(
        f"{host}.fs_read",
        {"path": path, "max_bytes": 256_000},
        cancel=asyncio.Event(),
        idempotency_key=f"attach-read:{current_run_id.get()}:{host}:{path}",
        timeout_s=60,
    )
    if res.kind.value == "error":
        return f"{host} could not read {path}: {res.text[:300]}"
    payload = json.loads(res.text)
    if payload.get("truncated"):
        return f"{path} is larger than {host} can hand over without fs_fetch (update the host)"
    return str(payload.get("text", "")).encode("utf-8"), None, name
