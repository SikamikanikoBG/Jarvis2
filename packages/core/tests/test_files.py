"""files.attach: a file under Jarvis's reply, for Arsen to open or download on his phone - what
Discord was used for until 2026-10-14."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import BaseModel

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import RoleName, ToolCall, ToolResult
from tests.conftest import Harness

REPORT = "<html><body><h1>Realty brief</h1><script>steal()</script></body></html>"


async def _run(harness: Harness, conversation_id: str, text: str) -> None:
    sub = harness.subscribe(conversation_id)
    await harness.core.engine.create_run(text=text, conversation_id=conversation_id)
    await harness.wait_for(sub, "run.done", timeout=10)


async def test_an_attached_file_comes_with_the_reply_and_is_not_fed_back_to_the_model(harness: Harness):
    core = harness.core
    conv = await core.store.create_conversation(title="Имоти")
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="a1", name="files.attach", arguments={"filename": "realty_brief.html", "content": REPORT})
            ]
        ),
        FakeTurn(text="Today's brief: 4 new listings. The full brief is attached."),
    )
    await _run(harness, conv.id, "направи брифа")
    msgs = await core.store.list_messages(conv.id)
    reply = msgs[-1]
    files = await core.attachments.for_message(reply.id or "")
    assert reply.role.value == "assistant" and [(f.name, f.mime, f.meta.get("outgoing")) for f in files] == [
        ("realty_brief.html", "text/html", True)
    ]
    # Next turn: the model is told it attached it - the HTML itself is not in its context again.
    harness.chat.push(FakeTurn(text="Yes."))
    await _run(harness, conv.id, "прати ли го?")
    sent = "\n".join(m.content for m in harness.chat.calls[-1][0])
    assert "[you attached realty_brief.html" in sent and "steal()" not in sent


async def test_a_file_on_a_host_is_fetched_whole_and_the_fetch_tool_is_never_offered(harness: Harness):
    core = harness.core
    pdf = b"%PDF-1.7 \x00\x01 binary"

    class _FetchArgs(BaseModel):
        path: str

    class Workspace(BuiltinProvider):
        name = "workspace"

        @tool("workspace.fs_fetch", description="internal", args=_FetchArgs)
        async def _fetch(self, path: str) -> ToolResult:
            return ToolResult.data(
                json.dumps({"name": "offer.pdf", "mime": "application/pdf", "base64": base64.b64encode(pdf).decode()})
            )

    core.registry.add(Workspace())
    await core.registry.refresh()
    spec = core.registry.get("workspace.fs_fetch")
    assert spec is not None
    spec.internal = True  # what an MCP host says with _meta {"jarvis/internal": true}

    conv = await core.store.create_conversation(title="Оферта")
    harness.chat.push(
        FakeTurn(
            tool_calls=[
                ToolCall(id="a1", name="files.attach", arguments={"host": "workspace", "path": "/data/offer.pdf"})
            ]
        ),
        FakeTurn(text="The offer is attached."),
    )
    await _run(harness, conv.id, "дай ми офертата")
    assert "workspace.fs_fetch" not in {t.name for t in harness.chat.calls[0][1]}
    reply = (await core.store.list_messages(conv.id))[-1]
    (att,) = await core.attachments.for_message(reply.id or "")
    assert att.name == "offer.pdf" and att.mime == "application/pdf" and att.bytes == len(pdf)
    raw = await core.attachments.raw(att.id)
    assert raw is not None and raw[0] == pdf


async def test_attach_says_what_is_missing(harness: Harness):
    conv = await harness.core.store.create_conversation()
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="a1", name="files.attach", arguments={"content": "x"})]),
        FakeTurn(tool_calls=[ToolCall(id="a2", name="files.attach", arguments={"host": "nowhere", "path": "/x"})]),
        FakeTurn(text="Could not."),
    )
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="attach", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    errors = [e.result.text for e in seen if e.type == "tool.result"]
    assert "filename" in errors[0] and "nowhere" in errors[1]


def test_a_page_is_served_sandboxed_and_download_saves_it(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as client:
        h = {"Authorization": "Bearer secret"}
        conv = client.post("/api/conversations", json={"title": "x"}, headers=h).json()
        # The app's own event loop runs in the client's portal: the store is called there.
        att = client.portal.call(  # type: ignore[union-attr]
            lambda: core.attachments.add_outgoing(
                data=REPORT.encode(), filename="brief.html", mime=None, conversation_id=conv["id"], run_id="r"
            )
        )
        shown = client.get(f"/api/attachments/{att.id}", headers=h)
        assert shown.status_code == 200 and shown.headers["content-security-policy"] == "sandbox"
        assert shown.headers["content-disposition"].startswith("inline")
        saved = client.get(f"/api/attachments/{att.id}?download=1", headers=h)
        assert saved.headers["content-disposition"].startswith("attachment") and saved.content == REPORT.encode()
