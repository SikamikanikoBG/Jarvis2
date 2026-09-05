from pathlib import Path

from jarvis_core.db import Database, Store
from jarvis_proto import Message, Run, RunKind, RunStatus, Settings
from jarvis_proto.events import RunStarted


async def test_migrations_apply_once(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.open()
    rows = await db.fetchall("SELECT version FROM schema_migrations")
    assert [r["version"] for r in rows] == [1]
    await db.close()
    db2 = Database(tmp_path / "t.db")
    await db2.open()  # idempotent
    rows = await db2.fetchall("SELECT version FROM schema_migrations")
    assert len(rows) == 1
    await db2.close()


async def test_conversation_messages_runs_events_roundtrip(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.open()
    store = Store(db)
    conv = await store.create_conversation(title="hello")
    await store.add_message(Message.user("hi", conversation_id=conv.id))
    run = await store.create_run(Run(id="run_1", conversation_id=conv.id, kind=RunKind.CHAT))
    ev = RunStarted(run_id=run.id, conversation_id=conv.id, seq=1)
    await store.append_event(ev)
    got = await store.get_run("run_1")
    assert got is not None and got.status is RunStatus.QUEUED
    events = await store.list_events("run_1")
    assert events[0]["type"] == "run.started"
    msgs = await store.list_messages(conv.id)
    assert [m.content for m in msgs] == ["hi"]
    c = await store.get_conversation(conv.id)
    assert c is not None and c.message_count == 1 and c.preview == "hi"
    await store.delete_conversation(conv.id)
    assert await store.get_run("run_1") is None  # cascade
    await db.close()


async def test_settings_partial_save(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.open()
    store = Store(db)
    s = Settings(user_name="A")
    await store.save_settings(s, only_keys={"user_name"})
    loaded = await store.load_settings()
    assert loaded.user_name == "A" and loaded.assistant_name == "Jarvis"
    await db.close()


async def test_idempotency_claim_once(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.open()
    store = Store(db)
    assert await store.claim_idempotency("k", "r", "t") is True
    assert await store.claim_idempotency("k", "r", "t") is False
    await db.close()
