"""The agentic browser leg (13 Sep 2026): the model SEES screenshots, a site it fought before
greets it with the playbook it learned, it can write that playbook itself, the reflector writes
one when it did not, and the supervisor's nudge names the tool instead of the loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.supervision import RunWatch, StepRecord, Supervisor, step_of
from jarvis_core.features.reflection import PlaybookReflector
from jarvis_core.features.skills import SkillStore, skill_document
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_core.models.openai_compat import to_openai_messages
from jarvis_core.tools.ws_provider import WsProvider
from jarvis_proto import (
    Attachment,
    AttachmentKind,
    Message,
    RoleName,
    RunBudget,
    ToolCall,
    ToolResult,
    ToolResultKind,
    ToolSpec,
)

# A 1x1 PNG is enough for "bytes went in, an image came out".
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token=None))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    with TestClient(create_app(core.config, core=core)) as c:
        c.fake = fake  # type: ignore[attr-defined]
        c.core = core  # type: ignore[attr-defined]
        yield c


def _tool(name: str, **extra):
    return {
        "name": name,
        "description": f"{name} does things. And more.",
        "input_schema": {"type": "object", "properties": {}},
        **extra,
    }


HELLO = {
    "type": "browser.hello",
    "agent": "jarvis-extension",
    "version": "2.0.0",
    "tools": [_tool("browser.open"), _tool("browser.screenshot", read_only=True), _tool("browser.click")],
}


def _run_until_done(ui, ext, answers: dict[str, dict]):
    """Answer each browser.call from `answers` (by tool name) on a thread and collect UI events
    until the run ends. Two sockets, two directions: neither may block the other."""
    import threading

    def serve():
        while True:
            try:
                call = json.loads(ext.receive_text())
            except Exception:
                return
            if call.get("type") != "browser.call":
                continue
            frame = dict(
                answers.get(call["name"])
                or {"kind": "error", "text": "no scripted answer", "error": "no scripted answer"}
            )
            frame.update({"type": "browser.result", "call_id": call["call_id"]})
            ext.send_text(json.dumps(frame))

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    events = []
    while True:
        ev = json.loads(ui.receive_text())
        events.append(ev)
        if ev["type"] in {"run.done", "run.failed"}:
            return events


# --- 1. the model sees what the browser saw -----------------------------------------------


def test_screenshot_reaches_the_model_as_an_image_and_the_transcript_as_an_attachment(client: TestClient):
    with client.websocket_connect("/ws?client=browser") as ext:
        ext.send_text(json.dumps(HELLO))
        assert json.loads(ext.receive_text())["type"] == "browser.ready"
        client.fake.push(  # type: ignore[attr-defined]
            FakeTurn(tool_calls=[ToolCall(id="c1", name="browser.screenshot", arguments={})]),
            FakeTurn(text="I see a login form."),
        )
        with client.websocket_connect("/ws") as ui:
            ui.send_text(json.dumps({"type": "run.create", "text": "what is on the page?"}))
            events = _run_until_done(
                ui,
                ext,
                {
                    "browser.screenshot": {
                        "kind": "data",
                        "text": "Screenshot captured (image/jpeg)",
                        "image": {"mime": "image/png", "base64": base64.b64encode(_PNG).decode()},
                    }
                },
            )
    assert events[-1]["type"] == "run.done"
    conv_id = events[-1]["conversation_id"]
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    tool_msg = next(m for m in msgs if m["role"] == "tool" and m["name"] == "browser.screenshot")
    assert len(tool_msg["attachments"]) == 1
    att = tool_msg["attachments"][0]
    assert att["kind"] == "image" and att["mime"].startswith("image/")
    # ...and it is served, like any attachment.
    assert client.get(f"/api/attachments/{att['id']}").status_code == 200
    # The model's second step was built with the picture in it: the fake adapter records what it
    # got (later calls are the titler and the learner — they see no tool messages).
    tool_seen = [m for msgs_, _ in client.fake.calls for m in msgs_ if m.role.value == "tool"]  # type: ignore[attr-defined]
    assert tool_seen and tool_seen[-1].attachments, "the tool message reached the adapter with its attachment"
    assert tool_seen[-1].attachments[0].data_url, "hydrated with a data: URL"


def test_openai_shape_puts_a_tool_image_in_a_user_turn_right_after_the_tool_message():
    att = Attachment(id="att_1", kind=AttachmentKind.IMAGE, name="screenshot.jpg", mime="image/jpeg", bytes=3)
    att.data_url = "data:image/jpeg;base64,AAA="
    msgs = [
        Message.user("look"),
        Message.tool("c1", "browser.screenshot", "Screenshot captured", attachments=[att]),
    ]
    out = to_openai_messages(msgs)
    assert [m["role"] for m in out] == ["user", "tool", "user"]
    parts = out[2]["content"]
    assert (
        parts[0]["type"] == "text"
        and "browser.screenshot" in parts[0]["text"]
        and "not a message from Arsen" in parts[0]["text"]
    )
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA="}}


def test_ws_provider_keeps_the_image_and_reports_a_changed_tool_set():
    p = WsProvider()

    async def send(_):
        return None

    p.connect(send, {"tools": [_tool("browser.open"), _tool("browser.click")]})
    assert p.tools_changed_note() is None  # the first hello is not a change
    p.connect(
        send, {"tools": [_tool("browser.open"), _tool("browser.click"), _tool("browser.wait"), _tool("browser.eval")]}
    )
    note = p.tools_changed_note()
    assert note is not None and "added browser.eval, browser.wait" in note
    assert "## Browser" in (p.context_block() or "") and "changed recently" in (p.context_block() or "")
    assert p.tools_changed_note(now=datetime.now(UTC) + timedelta(days=2)) is None

    async def roundtrip():
        task = asyncio.create_task(
            p.call("browser.screenshot", {}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=2)
        )
        await asyncio.sleep(0)
        call_id = next(iter(p._pending))
        p.handle_result(
            {"call_id": call_id, "kind": "data", "text": "shot", "image": {"mime": "image/jpeg", "base64": "QUJD"}}
        )
        return await task

    res = asyncio.run(roundtrip())
    assert res.kind is ToolResultKind.DATA and res.images and res.images[0].base64 == "QUJD"
    assert "images" not in json.loads(res.model_dump_json())  # never serialised onward


# --- 2. a site it has been to greets it with its playbook ---------------------------------


def test_site_playbook_rides_on_the_first_browser_result_for_that_host(client: TestClient):
    core: Core = client.core  # type: ignore[attr-defined]
    client.portal.call(
        lambda: core.skills.put(
            "webim-chat-dskbank",
            skill_document(
                description="DSK Bank Webim chat",
                triggers=["dsk chatbot"],
                sites=["dskbank.bg"],
                body="1. Send = svg.webim-ico-send next to the composer.\n2. Replies land in ~2 s: browser.wait.",
                extra={"learned": True},
            ),
        )
    )
    with client.websocket_connect("/ws?client=browser") as ext:
        ext.send_text(json.dumps(HELLO))
        assert json.loads(ext.receive_text())["type"] == "browser.ready"
        client.fake.push(  # type: ignore[attr-defined]
            FakeTurn(
                tool_calls=[
                    ToolCall(id="c1", name="browser.open", arguments={"url": "https://chatbot.dskbank.bg/client.php"})
                ]
            ),
            FakeTurn(tool_calls=[ToolCall(id="c2", name="browser.click", arguments={"ref": "@e9"})]),
            FakeTurn(text="done"),
        )
        with client.websocket_connect("/ws") as ui:
            # "dsk chatbot" is the skill's trigger: detection is structural, no model call eats a scripted turn
            ui.send_text(json.dumps({"type": "run.create", "text": "test the dsk chatbot"}))
            events = _run_until_done(
                ui,
                ext,
                {
                    "browser.open": {
                        "kind": "data",
                        "text": "Opened Онлайн-асистент — https://chatbot.dskbank.bg/client.php in the work tab",
                    },
                    "browser.click": {
                        "kind": "data",
                        "text": "[tab 1] Онлайн-асистент — https://chatbot.dskbank.bg/client.php\nClicked @e9",
                    },
                },
            )
    results = [e for e in events if e["type"] == "tool.result"]
    assert "Playbook `webim-chat-dskbank`" in results[0]["result"]["text"]
    assert "svg.webim-ico-send" in results[0]["result"]["text"]
    # once per run, not per click
    assert "Playbook" not in results[1]["result"]["text"]


def test_for_site_matches_subdomains_and_skips_disabled(tmp_path: Path):
    from jarvis_core.db import Database

    async def go():
        db = Database(tmp_path / "t.db")
        await db.open()
        try:
            store = SkillStore(tmp_path / "skills", db)
            await store.put("a", skill_document(description="A", triggers=[], sites=["dskbank.bg"], body="A body"))
            await store.put(
                "b", skill_document(description="B", triggers=[], sites=["https://www.other.com/x"], body="B body")
            )
            assert [n for n, _ in await store.for_site("https://chatbot.dskbank.bg/client.php")] == ["a"]
            assert [n for n, _ in await store.for_site("other.com")] == ["b"]
            assert await store.for_site("dskbank.bg.evil.com") == []
            await store.set_enabled("a", False)
            assert await store.for_site("dskbank.bg") == []
        finally:
            await db.close()

    asyncio.run(go())


# --- 3. it can write the playbook itself ---------------------------------------------------


def test_skills_learn_writes_a_playbook_and_refuses_to_overwrite_a_hand_written_skill(client: TestClient):
    core: Core = client.core  # type: ignore[attr-defined]
    client.portal.call(lambda: core.skills.put("manual", "---\ndescription: hand-made\n---\nkeep me"))

    async def call(args):
        return await core.registry.call("skills.learn", args, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)

    res = client.portal.call(
        lambda: call(
            {
                "name": "webim-chat-dskbank",
                "description": "DSK Bank Webim chat",
                "body": "Send = svg.webim-ico-send; wait ~2 s for replies.",
                "triggers": ["dsk chatbot"],
                "sites": ["dskbank.bg"],
            }
        )
    )
    assert res.kind is ToolResultKind.DATA and "learned for dskbank.bg" in res.text
    content = client.portal.call(lambda: core.skills.get("webim-chat-dskbank"))
    meta, body = SkillStore.parse(content)
    assert meta["learned"] is True and meta["sites"] == ["dskbank.bg"] and "svg.webim-ico-send" in body
    listed = {s["name"] for s in client.get("/api/skills").json()}
    assert "webim-chat-dskbank" in listed

    res2 = client.portal.call(
        lambda: call({"name": "manual", "description": "x", "body": "overwrite", "triggers": [], "sites": []})
    )
    assert res2.kind is ToolResultKind.ERROR and "hand-written" in res2.text
    assert "keep me" in client.portal.call(lambda: core.skills.get("manual"))


# --- 4. and when it did not, the reflector does ---------------------------------------------


def _steps(*items: tuple[str, str, str]) -> list[StepRecord]:
    out = []
    for tool, kind, text in items:
        res = ToolResult.failure(text) if kind == "error" else ToolResult.data(text)
        out.append(step_of(tool, {"ref": "@e9"} if tool == "browser.click" else {}, res))
    return out


def test_reflector_only_fires_on_struggle_then_success_and_writes_a_site_playbook(tmp_path: Path):
    from jarvis_core.db import Database

    quiet = _steps(
        ("browser.open", "data", "Opened https://x.test/"),
        ("browser.read", "data", "text"),
        ("browser.click", "data", "ok"),
        ("browser.read", "data", "t"),
    )
    fought = _steps(
        ("browser.open", "data", "Opened https://chatbot.dskbank.bg/client.php"),
        ("browser.click", "error", "no result came back from the page for click"),
        ("browser.click", "error", "no result came back from the page for click"),
        ("browser.eval", "data", '[tab 1] https://chatbot.dskbank.bg/client.php\n"sent"'),
        ("browser.wait", "data", "The page changed after 2s. New text: Лихвата е 0.5%"),
    )
    fake = FakeAdapter()
    fake.default_turn = FakeTurn(
        text=json.dumps(
            {
                "name": "Webim Chat DSKBank!",
                "description": "DSK Bank Webim chat widget",
                "sites": ["dskbank.bg"],
                "triggers": ["dsk chatbot", "webim"],
                "body": "1. Send: browser.eval dispatch click on svg.webim-ico-send.\n2. browser.wait ~2 s for the reply.",
            }
        )
    )

    async def go():
        db = Database(tmp_path / "t.db")
        await db.open()
        try:
            store = SkillStore(tmp_path / "skills", db)
            r = PlaybookReflector(store, lambda: fake)
            assert r.worth_it(quiet) is None
            assert r.worth_it(fought) == "chatbot.dskbank.bg"
            learned: list[tuple[str, str]] = []
            r.schedule(fought, on_learned=lambda n, d: learned.append((n, d)))
            await asyncio.gather(*r._tasks)
            assert learned == [("webim-chat-dskbank", "DSK Bank Webim chat widget")]
            names = [n for n, _ in await store.for_site("https://chatbot.dskbank.bg/x")]
            assert names == ["webim-chat-dskbank"]
            meta, body = SkillStore.parse(await store.get("webim-chat-dskbank") or "")
            assert meta["learned"] is True and "learned_at" in meta and "svg.webim-ico-send" in body
            # a second run on the site UPDATES the same playbook instead of minting another
            r.schedule(fought, on_learned=lambda n, d: learned.append((n, d)))
            await asyncio.gather(*r._tasks)
            assert len(list((tmp_path / "skills").glob("*.md"))) == 1
        finally:
            await db.close()

    asyncio.run(go())


# --- 5. the supervisor names the door --------------------------------------------------------


def test_supervisor_lists_the_unused_tools_of_the_family_it_is_stuck_in():
    catalog = [
        ToolSpec(name="browser.read", description="Read the page. Long tail."),
        ToolSpec(name="browser.wait", description="Wait for the page to change. Long tail."),
        ToolSpec(name="browser.eval", description="Run JavaScript in the page. Long tail."),
        ToolSpec(name="notes.add", description="Add a note."),
    ]
    sup = Supervisor(lambda: None, FakeAdapter, catalog=lambda: catalog)  # type: ignore[arg-type]
    watch = RunWatch(budget=RunBudget())
    watch.steps = _steps(
        ("browser.read", "data", "same"), ("browser.read", "data", "same"), ("browser.read", "data", "same")
    )
    block = sup._alternatives(watch)
    assert "- browser.wait: Wait for the page to change" in block
    assert "- browser.eval: Run JavaScript in the page" in block
    assert "browser.read" not in block and "notes.add" not in block
    assert Supervisor(lambda: None, FakeAdapter)._alternatives(watch) == ""  # type: ignore[arg-type]
