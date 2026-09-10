"""Incognito and disappearing chats: what leaves them (nothing), and when they leave (on time).

An incognito chat is a promise, so every passive path that would carry its words elsewhere is
checked here one by one - the knowledge learner, the titler, search, the sidebar preview, the
memory tools offered to the model - and the sweeper that finally removes it is checked to remove
everything, attachment files included.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.loop import is_memory_writer
from jarvis_core.features.expiry import valid_ttl
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import INCOGNITO_TITLE, TTL_CHOICES, Message, RoleName, ToolCall, ToolSpec
from tests.conftest import Harness

# --- store -------------------------------------------------------------------------------


async def test_a_ttl_sets_an_expiry_and_every_message_pushes_it_out(harness: Harness):
    store = harness.core.store
    conv = await store.create_conversation(ttl_seconds=3_600)
    assert conv.ttl_seconds == 3_600 and conv.expires_at is not None
    assert abs((conv.expires_at - conv.created_at).total_seconds() - 3_600) < 1
    first = conv.expires_at

    await store.add_message(Message.user("still here", conversation_id=conv.id))
    touched = await store.get_conversation(conv.id)
    assert touched and touched.expires_at is not None and touched.expires_at >= first
    # "Idle" is measured from the last message: the expiry is roughly an hour from NOW.
    assert abs((touched.expires_at - datetime.now(UTC)).total_seconds() - 3_600) < 5

    # Changing the timer restarts it from now; null keeps the chat for good.
    conv = await store.update_conversation(conv.id, ttl_seconds=86_400)
    assert conv and conv.ttl_seconds == 86_400 and conv.expires_at is not None
    assert abs((conv.expires_at - datetime.now(UTC)).total_seconds() - 86_400) < 5
    conv = await store.update_conversation(conv.id, ttl_seconds=None)
    assert conv and conv.ttl_seconds is None and conv.expires_at is None

    # A plain chat has no timer at all.
    plain = await store.create_conversation()
    assert plain.ttl_seconds is None and plain.expires_at is None and plain.incognito is False


async def test_an_incognito_chat_is_never_previewed_or_found_and_stays_unless_timed(harness: Harness):
    store = harness.core.store
    conv = await store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    # Incognito is about memory, not lifetime: no timer unless one is asked for.
    assert conv.incognito and conv.ttl_seconds is None and conv.expires_at is None
    timed = await store.create_conversation(title=INCOGNITO_TITLE, incognito=True, ttl_seconds=3_600)
    assert timed.incognito and timed.ttl_seconds == 3_600 and timed.expires_at is not None

    await store.add_message(Message.user("my secret mango allergy", conversation_id=conv.id))
    await store.add_message(Message.assistant("Noted for this chat only.", conversation_id=conv.id))
    got = await store.get_conversation(conv.id)
    assert got and got.message_count == 2 and got.preview is None  # the row never quotes it

    # Search: neither the title nor the text of an incognito chat is ever a hit.
    other = await store.create_conversation(title="Mango recipes")
    await store.add_message(Message.user("mango lassi", conversation_id=other.id))
    hits = await store.search("mango")
    assert [h.conversation.id for h in hits] == [other.id]
    assert await store.search("secret") == []

    # A fork of a private chat is private too, and on the same timer (or none).
    fork = await store.fork_conversation(timed.id, up_to_message_id=None)
    assert fork and fork.incognito and fork.ttl_seconds == 3_600 and fork.preview is None
    fork = await store.fork_conversation(conv.id, up_to_message_id=None)
    assert fork and fork.incognito and fork.ttl_seconds is None


async def test_expired_conversations_are_the_ones_past_their_time(harness: Harness):
    store = harness.core.store
    due = await store.create_conversation(ttl_seconds=3_600)
    later = await store.create_conversation(ttl_seconds=3_600)
    kept = await store.create_conversation()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await store.update_conversation(due.id, expires_at=past)
    ids = [c.id for c in await store.expired_conversations()]
    assert ids == [due.id]
    assert later.id not in ids and kept.id not in ids


def test_only_the_three_offered_idle_times_are_valid():
    assert [valid_ttl(t) for t in TTL_CHOICES] == list(TTL_CHOICES)
    assert valid_ttl(None) is None and valid_ttl(0) is None and valid_ttl(-1) is None and valid_ttl(3_601) is None


# --- reaper --------------------------------------------------------------------------------


async def test_the_reaper_removes_a_due_chat_with_its_files_and_tells_every_client(harness: Harness):
    core = harness.core
    conv = await core.store.create_conversation(ttl_seconds=3_600)
    att = await core.attachments.add_file(
        data=b"a note to self", filename="note.txt", mime="text/plain", conversation_id=conv.id
    )
    stored = await core.attachments.get(att.id)
    assert stored and stored.path and stored.path.exists()
    sub = harness.subscribe(conv.id)
    await core.store.update_conversation(conv.id, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())

    removed = await core.reaper.sweep()
    assert removed == [conv.id]
    assert await core.store.get_conversation(conv.id) is None
    assert not stored.path.exists()  # the file, not only the row
    seen = await harness.wait_for(sub, "conversation.deleted")
    assert seen[-1].conversation_id == conv.id
    # Nothing due: nothing removed, nothing broken.
    assert await core.reaper.sweep() == []


async def test_the_reaper_waits_for_a_chat_that_is_still_working(harness: Harness):
    core = harness.core
    harness.chat.push(FakeTurn(hang=True))
    conv = await core.store.create_conversation(ttl_seconds=3_600)
    sub = harness.subscribe(conv.id)
    run, _ = await core.engine.create_run(text="think about it", conversation_id=conv.id)
    await harness.wait_for(sub, "model.call")
    await core.store.update_conversation(conv.id, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    assert await core.reaper.sweep() == []  # a run is in flight: left alone
    assert await core.store.get_conversation(conv.id) is not None
    await core.engine.cancel(run.id)
    await harness.wait_for(sub, "run.cancelled")
    # Nothing was streamed before the stop, so no message re-armed the timer: the run is over
    # and the time is up, and the next round takes it.
    assert await core.reaper.sweep() == [conv.id]
    assert await core.store.get_conversation(conv.id) is None


# --- engine --------------------------------------------------------------------------------


def test_memory_writers_are_the_notes_and_knowledge_mutations():
    assert is_memory_writer(ToolSpec(name="notes.add"))
    assert is_memory_writer(ToolSpec(name="kg.remember"))
    assert not is_memory_writer(ToolSpec(name="notes.list", read_only=True))
    assert not is_memory_writer(ToolSpec(name="kg.who_is", read_only=True))
    assert not is_memory_writer(ToolSpec(name="schedule.create"))
    assert not is_memory_writer(ToolSpec(name="workocholic.outlook_send", destructive=True))


async def test_an_incognito_run_is_not_titled_not_learned_from_and_has_no_memory_tools(harness: Harness):
    core = harness.core
    core.engine.set_titler(core.titler.title)
    harness.enable(kg_learning=True)
    # One scripted turn: the reply. A title or a learning call would be a second model call.
    long_reply = "I hear you. That sounds heavy, and it is fine to say so here. " * 2
    harness.chat.push(FakeTurn(text=long_reply))
    conv = await core.store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(
        text="something very personal I do not want remembered anywhere else", conversation_id=conv.id
    )
    await harness.wait_for(sub, "run.done")
    await core.learner.wait()

    assert len(harness.chat.calls) == 1, [m[0][-1].content[:60] for m in harness.chat.calls]
    got = await core.store.get_conversation(conv.id)
    assert got and got.title == INCOGNITO_TITLE and got.preview is None
    # What the model was offered: no notes.add, no kg.remember - reading them is still fine.
    offered = {t.name for t in harness.chat.calls[0][1]}
    assert "notes.add" not in offered and "kg.remember" not in offered
    assert "notes.list" in offered and "kg.who_is" in offered
    # And it was told why, in the stable part of the system message.
    system = harness.chat.calls[0][0][0]
    assert system.role.value == "system" and "## Private conversation" in system.content
    assert await core.db.fetchall("SELECT name FROM kg_entities") == []


async def test_a_plain_chat_still_learns_so_the_incognito_test_means_something(harness: Harness):
    core = harness.core
    harness.enable(kg_learning=True)
    harness.chat.push(
        FakeTurn(text="Rumen runs the Q4 budget review on Friday; the deck is due Thursday night. " * 2),
        FakeTurn(text=json.dumps({"entities": [{"name": "Rumen", "type": "person", "summary": "runs the budget"}]})),
    )
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(
        text="who runs the Q4 budget review and when is the deck due?", conversation_id=conv.id
    )
    await harness.wait_for(sub, "run.done")
    await core.learner.wait()
    assert len(harness.chat.calls) == 2
    assert [r["name"] for r in await core.db.fetchall("SELECT name FROM kg_entities")] == ["Rumen"]


async def test_a_remembered_tool_call_in_an_incognito_chat_is_refused_not_run(harness: Harness):
    core = harness.core
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="notes.add", arguments={"board": "Private", "text": "secret"})]),
        FakeTurn(text="I cannot save that here."),
    )
    conv = await core.store.create_conversation(title=INCOGNITO_TITLE, incognito=True)
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="pin this: my secret", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done")
    results = [e for e in seen if e.type == "tool.result"]
    assert results and results[0].result.kind.value == "error" and "incognito" in results[0].result.text
    assert await core.boards.list_boards() == []  # nothing was written


# --- REST + WS -----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        c.fake = fake  # type: ignore[attr-defined]
        yield c


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_rest_creates_patches_and_refuses_the_right_things(client: TestClient):
    r = client.post("/api/conversations", json={"incognito": True}, headers=_h())
    assert r.status_code == 201
    inc = r.json()
    assert inc["incognito"] is True and inc["title"] == INCOGNITO_TITLE
    assert inc["ttl_seconds"] is None and inc["expires_at"] is None  # kept, unless a timer is asked for
    # A timer can be added and taken away again like on any other chat.
    r = client.patch(f"/api/conversations/{inc['id']}", json={"ttl_seconds": 86_400}, headers=_h())
    assert r.status_code == 200 and r.json()["ttl_seconds"] == 86_400 and r.json()["incognito"] is True
    r = client.patch(f"/api/conversations/{inc['id']}", json={"ttl_seconds": None}, headers=_h())
    assert r.status_code == 200 and r.json()["ttl_seconds"] is None and r.json()["expires_at"] is None
    # Renaming is allowed - the name is Arsen's, not the classifier's - and does not un-incognito it.
    r = client.patch(f"/api/conversations/{inc['id']}", json={"title": "Just us"}, headers=_h())
    assert r.json()["title"] == "Just us" and r.json()["incognito"] is True

    r = client.post("/api/conversations", json={"title": "Quick", "ttl_seconds": 3_600}, headers=_h())
    assert r.status_code == 201 and r.json()["ttl_seconds"] == 3_600 and r.json()["incognito"] is False
    plain = r.json()["id"]
    assert client.patch(f"/api/conversations/{plain}", json={"ttl_seconds": 12_345}, headers=_h()).status_code == 422
    r = client.patch(f"/api/conversations/{plain}", json={"ttl_seconds": None}, headers=_h())
    assert r.status_code == 200 and r.json()["ttl_seconds"] is None and r.json()["expires_at"] is None
    # Leaving the key out leaves the timer alone.
    r = client.patch(f"/api/conversations/{plain}", json={"ttl_seconds": 604_800}, headers=_h())
    assert r.json()["ttl_seconds"] == 604_800
    assert (
        client.patch(f"/api/conversations/{plain}", json={"pinned": True}, headers=_h()).json()["ttl_seconds"]
        == 604_800
    )
    assert client.post("/api/conversations", json={"ttl_seconds": 5}, headers=_h()).status_code == 422

    # The search route never returns the incognito chat, whatever it is called.
    assert [h["conversation"]["id"] for h in client.get("/api/search?q=Just", headers=_h()).json()] == []
    assert [h["conversation"]["id"] for h in client.get("/api/search?q=Quick", headers=_h()).json()] == [plain]


def _new_conversation(ws, known: set[str]) -> dict:  # type: ignore[no-untyped-def]
    """The first conversation.updated for a conversation not seen yet (the finished run's own
    late events - run.updated, its conversation.updated - are still arriving)."""
    while True:
        ev = json.loads(ws.receive_text())
        if ev["type"] == "conversation.updated" and ev["conversation"]["id"] not in known:
            known.add(ev["conversation"]["id"])
            return ev["conversation"]


def _until_done(ws) -> None:  # type: ignore[no-untyped-def]
    while json.loads(ws.receive_text())["type"] != "run.done":
        pass


def test_ws_first_message_opens_an_incognito_or_disappearing_chat(client: TestClient):
    client.fake.push(FakeTurn(text="only between us"), FakeTurn(text="quick answer"))  # type: ignore[attr-defined]
    known: set[str] = set()
    with client.websocket_connect("/ws?token=secret") as ws:
        ws.send_text(json.dumps({"type": "run.create", "text": "hi", "incognito": True}))
        conv = _new_conversation(ws, known)
        assert conv["incognito"] is True and conv["title"] == INCOGNITO_TITLE and conv["ttl_seconds"] is None
        _until_done(ws)
        after = client.get(f"/api/conversations/{conv['id']}", headers=_h()).json()
        assert after["title"] == INCOGNITO_TITLE and after["preview"] is None  # untouched by the first message

        ws.send_text(json.dumps({"type": "run.create", "text": "what day is it", "ttl_seconds": 86_400}))
        conv = _new_conversation(ws, known)
        assert conv["incognito"] is False and conv["ttl_seconds"] == 86_400
        _until_done(ws)
        after = client.get(f"/api/conversations/{conv['id']}", headers=_h()).json()
        assert after["title"] == "what day is it" and after["preview"] == "quick answer"

        # A ttl that is not offered is dropped rather than failing the message.
        ws.send_text(json.dumps({"type": "run.create", "text": "hello", "ttl_seconds": 99}))
        conv = _new_conversation(ws, known)
        assert conv["ttl_seconds"] is None and conv["incognito"] is False
