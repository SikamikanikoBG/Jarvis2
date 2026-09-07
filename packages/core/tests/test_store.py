from pathlib import Path

from jarvis_core.db import Database, Store
from jarvis_proto import Message, Run, RunKind, RunStatus, Settings, ToolSpec
from jarvis_proto.events import RunStarted


async def test_migrations_apply_once(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.open()
    rows = await db.fetchall("SELECT version FROM schema_migrations")
    versions = [r["version"] for r in rows]
    assert versions == sorted(versions) and versions[0] == 1 and len(versions) >= 2
    await db.close()
    db2 = Database(tmp_path / "t.db")
    await db2.open()  # idempotent
    rows = await db2.fetchall("SELECT version FROM schema_migrations")
    assert len(rows) == len(versions)
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


async def test_provider_tools_survive_a_restart(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.open()
    store = Store(db)
    specs = [ToolSpec(name="workocholic.outlook_send", description="send mail", provider="workocholic")]
    await store.save_provider_tools("workocholic", specs)
    await store.save_provider_tools("workocholic", specs)  # upsert, not a second row
    await db.close()

    db2 = Database(tmp_path / "t.db")
    await db2.open()
    loaded = await Store(db2).load_provider_tools()
    assert list(loaded) == ["workocholic"]
    assert loaded["workocholic"] == specs  # description and hints included, not just names

    # A row this code cannot read is skipped, not fatal: the tool list would be empty either
    # way, and a core that will not start is worse.
    await db2.execute("UPDATE provider_tools SET specs = ? WHERE provider = ?", ("not json", "workocholic"))
    assert await Store(db2).load_provider_tools() == {}
    await Store(db2).forget_provider_tools("workocholic")
    assert await db2.fetchall("SELECT provider FROM provider_tools") == []
    await db2.close()
