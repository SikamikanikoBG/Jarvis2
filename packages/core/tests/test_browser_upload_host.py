"""browser.upload from a file on a host: the core fetches it and hands the extension its bytes.

The extension can attach a `url` it can fetch or base64 `data`. A report Jarvis built in the
workspace is neither: the browser on jarvisvm cannot reach the workspace container, and a 4 MB
file as base64 is far too big to pass through the model. So the daily portfolio run uploaded
the report to catbox.moe, a public file host, to get a URL (2026-10-18). With `host` + `path`
the file goes from the host to the browser through the core, never through the model or a
public host.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from jarvis_core.engine.current import current_conversation_id
from jarvis_core.tools.ws_provider import WsProvider

UPLOAD = {
    "name": "browser.upload",
    "description": "Attach a file to an upload field",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}, "data": {"type": "string"}, "filename": {"type": "string"}},
    },
}


class Ext:
    def __init__(self, provider: WsProvider) -> None:
        self.provider = provider
        self.calls: list[dict[str, Any]] = []
        provider.connect(self.send, {"type": "browser.hello", "agent": "jarvis-extension", "tools": [UPLOAD]})

    async def send(self, frame: dict[str, Any]) -> None:
        if frame["type"] == "browser.call":
            self.calls.append(frame)
            asyncio.get_running_loop().call_soon(
                self.provider.handle_result, {"call_id": frame["call_id"], "kind": "data", "text": "attached"}
            )


async def upload(provider: WsProvider, args: dict[str, Any]) -> Any:
    token = current_conversation_id.set("chat")
    try:
        return await provider.call("browser.upload", args, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    finally:
        current_conversation_id.reset(token)


async def test_a_file_on_a_host_reaches_the_upload_field_as_bytes():
    asked: list[tuple[str, str]] = []

    async def fetch(host: str, path: str) -> tuple[bytes, str | None, str] | str:
        asked.append((host, path))
        return b"<html>report</html>", "text/html", "portfolio_report.html"

    provider = WsProvider(fetch_file=fetch)
    ext = Ext(provider)
    (spec,) = await provider.list_tools()
    assert {"host", "path"} <= set(spec.input_schema["properties"])

    result = await upload(provider, {"host": "workspace", "path": "/workspace/analysis/portfolio_report.html"})
    assert result.text == "attached"
    assert asked == [("workspace", "/workspace/analysis/portfolio_report.html")]
    sent = ext.calls[-1]["arguments"]
    assert "host" not in sent and "path" not in sent
    assert base64.b64decode(sent["data"]) == b"<html>report</html>"
    assert sent["filename"] == "portfolio_report.html" and sent["mime"] == "text/html"

    # A name he chose wins over the file's own.
    await upload(provider, {"host": "workspace", "path": "/w/r.html", "filename": "Portfolio 19.10.html"})
    assert ext.calls[-1]["arguments"]["filename"] == "Portfolio 19.10.html"


async def test_a_file_the_host_cannot_hand_over_is_said_and_nothing_is_attached():
    async def fetch(host: str, path: str) -> tuple[bytes, str | None, str] | str:
        return f"{host} could not hand over {path}: not found"

    provider = WsProvider(fetch_file=fetch)
    ext = Ext(provider)
    result = await upload(provider, {"host": "workspace", "path": "/nope.html"})
    assert result.kind.value == "error" and "not found" in result.text
    assert ext.calls == []
    # And it cannot be both: a host file and a url.
    both = await upload(provider, {"host": "workspace", "path": "/a.html", "url": "https://x/a.html"})
    assert both.kind.value == "error" and ext.calls == []
