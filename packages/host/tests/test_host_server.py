"""The MCP server end to end: bearer gate, tool annotations, calls over streamable HTTP."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from fake_com import World
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from jarvis_host.config import HostConfig
from jarvis_host.server import BearerAuth, build_app, make_deps

TOKEN = "s3cret-token"

EXPECTED_ANNOTATIONS = {
    "outlook_accounts": ("read", True),
    "outlook_folders": ("read", True),
    "outlook_list": ("read", True),
    "outlook_read": ("read", True),
    "outlook_search": ("read", True),
    "outlook_move": ("mutating", False),
    "outlook_folder_create": ("mutating", True),
    "outlook_flag": ("mutating", True),
    "outlook_send": ("destructive", False),
    "calendar_list": ("read", True),
    "calendar_create": ("destructive", False),
    "calendar_delete": ("destructive", False),
    "calendar_invites": ("read", True),
    # Sends a response to the organizer: mutating, repeatable in effect but not idempotent.
    "calendar_respond": ("mutating", False),
    "calendar_free_slots": ("read", True),
    "calendar_remove_canceled": ("destructive", False),
    "onenote_tree": ("read", True),
    "onenote_read": ("read", True),
    "onenote_search": ("read", True),
    "onenote_create": ("mutating", False),
    "onenote_append": ("mutating", False),
    "onenote_move": ("mutating", False),
    "meeting_start": ("mutating", False),
    "meeting_pull": ("read", True),
    "meeting_stop": ("mutating", False),
    "fs_list": ("read", True),
    "fs_read": ("read", True),
    "fs_write": ("destructive", False),
    "fs_edit": ("destructive", False),
    "fs_search": ("read", True),
    "shell_run": ("destructive", False),
    "volume_get": ("read", True),
    # Mutating but not destructive: setting the volume is trivially reversible, so it should
    # not stop an interactive run for a confirmation.
    "volume_set": ("mutating", True),
    "screen_grab": ("read", True),
    "host_status": ("read", True),
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def server(world: World, tmp_path: Path) -> Iterator[tuple[str, BearerAuth]]:
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    port = _free_port()
    cfg = HostConfig(
        name="testhost",
        listen=f"127.0.0.1:{port}",
        token=TOKEN,
        fs_roots=(tmp_path,),
        shell_allow=True,
        screen_enabled=False,
    )
    deps = make_deps(cfg, dispatch=world.dispatch)
    deps.shell.launcher = [sys.executable, "-c"]
    deps.status.process_state = lambda: None  # do not consult this machine's real OUTLOOK.EXE
    app = build_app(cfg, deps)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        srv.should_exit = True
        thread.join(10)


async def test_healthz_is_open_and_mcp_is_gated(server: tuple[str, BearerAuth]):
    base, _ = server
    async with httpx.AsyncClient(timeout=10) as http:
        health = await http.get(f"{base}/healthz")
        assert health.status_code == 200 and health.json()["name"] == "testhost"
        anon = await http.post(f"{base}/mcp", json={})
        assert anon.status_code == 401 and anon.headers["www-authenticate"] == "Bearer"
        wrong = await http.post(f"{base}/mcp", json={}, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = await http.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"},
        )
        assert ok.status_code != 401  # past the gate; the transport itself decides what to answer


async def test_tools_annotations_and_calls(server: tuple[str, BearerAuth], world: World, tmp_path: Path):
    base, app = server
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=httpx.Timeout(30, read=60)) as http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert set(tools) == set(EXPECTED_ANNOTATIONS)
        for name, (kind, idempotent) in EXPECTED_ANNOTATIONS.items():
            ann = tools[name].annotations
            assert ann is not None, name
            assert ann.readOnlyHint is (kind == "read"), name
            assert ann.destructiveHint is (kind == "destructive"), name
            assert ann.idempotentHint is idempotent, name
            assert tools[name].description, name
        assert tools["outlook_list"].inputSchema["properties"]["limit"]["default"] == 25
        assert "cursor" in tools["outlook_list"].inputSchema["properties"]

        status = json.loads((await session.call_tool("host_status", {})).content[0].text)  # type: ignore[union-attr]
        assert status["name"] == "testhost" and status["outlook"]["connected"] is True and status["com"]["alive"]
        assert status["outlook"]["accounts"] == ["aapostolov@postbank.bg", "arsen@gmail.com"]

        listing = json.loads((await session.call_tool("outlook_list", {"folder": "inbox", "limit": 2})).content[0].text)  # type: ignore[union-attr]
        assert len(listing["items"]) == 2 and listing["cursor"] and listing["total"] == 7

        moved = await session.call_tool(
            "outlook_move", {"entry_id": listing["items"][0]["entry_id"], "folder": "Demands/DM-1234"}
        )
        assert not moved.isError and json.loads(moved.content[0].text)["folder"].endswith("\\DM-1234")  # type: ignore[union-attr]
        assert world.mails[0].Parent is world.dm1234

        bad = await session.call_tool("outlook_read", {"entry_id": "0" * 48})
        assert bad.isError and "not found" in bad.content[0].text  # type: ignore[union-attr]

        files = json.loads((await session.call_tool("fs_list", {"path": str(tmp_path)})).content[0].text)  # type: ignore[union-attr]
        assert [e["name"] for e in files["entries"]] == ["hello.txt"]
        outside = await session.call_tool("fs_read", {"path": str(tmp_path.parent / "nope.txt")})
        assert outside.isError and "outside the allowed roots" in outside.content[0].text  # type: ignore[union-attr]

        shell = json.loads(
            (
                await session.call_tool(
                    "shell_run", {"command": "print('hey')"}, read_timeout_seconds=timedelta(seconds=30)
                )
            )
            .content[0]
            .text
        )  # type: ignore[union-attr]
        assert shell["exit_code"] == 0 and shell["stdout"] == "hey\n"

        grab = await session.call_tool("screen_grab", {})
        assert grab.isError and "disabled" in grab.content[0].text  # type: ignore[union-attr]

    deps = app.app.state.deps
    assert deps.worker.status().completed >= 4


def test_shutdown_releases_the_microphone(world: World, tmp_path: Path):
    """A recording still running holds the mic and the loopback device.

    The lifespan stopped the COM worker and left MeetingCapture alone, so restarting the host
    mid-meeting left the mic light on and the device claimed until the process was killed.
    """
    from starlette.testclient import TestClient

    from jarvis_host.meetings import MeetingCapture

    closed: list[str] = []

    class FakeSource:
        rate, channels, peak = 16_000, 1, 500

        def __init__(self, name: str) -> None:
            self.name = name

        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            closed.append(self.name)

    cfg = HostConfig(name="testhost", token=TOKEN, fs_roots=(tmp_path,))
    deps = make_deps(cfg, dispatch=world.dispatch)
    deps.meetings = MeetingCapture(lambda wanted: ({n: FakeSource(n) for n in wanted}, []))
    deps.status.process_state = lambda: None
    app = build_app(cfg, deps)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        deps.meetings.start("mtg_1", "mic,system")
        assert deps.meetings.active() == ["mtg_1"]
        assert closed == []
    # Leaving the context runs the lifespan's shutdown.
    assert sorted(closed) == ["mic", "system"]
    assert deps.meetings.active() == []


def test_transport_security_wildcard_disables_the_guard_and_a_list_enables_it():
    """The SDK matches exact hosts and `host:*` only, so "*" must turn the guard off, not
    be passed through as an allow-list entry that never matches (a live 421 from ardi)."""
    from jarvis_host.config import HostConfig
    from jarvis_host.server import transport_security

    wild = transport_security(HostConfig(name="t", token="x"))
    assert wild.enable_dns_rebinding_protection is False

    listed = transport_security(HostConfig(name="t", token="x", allowed_hosts=("100.75.37.17:9030", "localhost:*")))
    assert listed.enable_dns_rebinding_protection is True
    assert "100.75.37.17:9030" in listed.allowed_hosts and "localhost:*" in listed.allowed_hosts
