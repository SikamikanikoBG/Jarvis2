"""Long-term memory, step 1: what was said, searchable (features/memory.py, migration 0023)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jarvis_core.engine.current import current_run_id
from jarvis_core.features.memory import MemoryTools, match_query
from jarvis_core.models.fake import FakeTurn
from jarvis_proto import ConversationKind, Message, Role, RunKind, ToolCall
from tests.conftest import Harness

FRIDAY = datetime(2026, 11, 13, 14, 45, tzinfo=UTC)


async def _say(harness: Harness, conv_id: str, text: str, at: datetime, role: Role = Role.USER, **kw: object) -> str:
    m = Message(role=role, content=text, conversation_id=conv_id, created_at=at, **kw)
    await harness.core.store.add_message(m)
    assert m.id
    return m.id


async def _recall(harness: Harness, **args: object) -> str:
    result = await MemoryTools(harness.core).call(
        "memory.recall", args, cancel=asyncio.Event(), idempotency_key="k", timeout_s=30
    )
    return result.text or result.error or ""


def test_the_words_become_prefixes_and_no_operator_gets_through() -> None:
    assert match_query(["Гюзелев", "A-Flow"]) == '"Гюзелев"* OR "A" + "Flow"*'
    assert match_query(['"; DROP', "NEAR(a b)", "  ", "*"]) == '"DROP"* OR "NEAR" + "a" + "b"*'
    assert match_query([]) == ""


async def test_recall_finds_what_was_said_in_the_same_long_conversation(harness: Harness):
    """2026-11-17: 'what did Niki Gyuzelev and I say last Friday' - it stood 3,000 messages up in
    the same conversation and Jarvis had 'no record'."""
    store = harness.core.store
    home = await store.create_conversation(title="Jarvis")
    await _say(harness, home.id, "Срещнах се с Ники Гюзелев за A-Flow, продължи два часа.", FRIDAY)
    await _say(harness, home.id, "И какво реши той?", FRIDAY + timedelta(minutes=1), Role.ASSISTANT)
    await _say(harness, home.id, "Иска enterprise инфраструктура.", FRIDAY + timedelta(minutes=2))
    for i in range(30):
        await _say(harness, home.id, f"нещо друго {i}", FRIDAY + timedelta(days=1, minutes=i))

    found = await _recall(harness, words=["Гюзелев", "Gyuzelev"], since="2026-11-13", until="2026-11-13")
    assert "продължи два часа" in found and "И какво реши той?" in found and "enterprise" in found
    assert "Fri 2026-11-13 16:45" in found, "his time zone, with the weekday"
    assert "Arsen:" in found and "Jarvis:" in found
    assert "нещо друго" not in found, "outside the days asked for"
    # Inflected forms are found by their beginning; a word never said is an empty answer, not an error.
    assert "продължи два часа" in await _recall(harness, words=["Гюзел"])
    assert "Nothing said matches" in await _recall(harness, words=["Фибанк"])


async def test_recall_by_day_alone_is_his_day_not_the_machines(harness: Harness):
    store = harness.core.store
    chat = await store.create_conversation(title="Домо")
    job = await store.create_conversation(title="Morning brief", kind=ConversationKind.SCHEDULED)
    await _say(harness, chat.id, "планираме за Ники", FRIDAY)
    await _say(harness, job.id, "Run the morning brief", FRIDAY)
    found = await _recall(harness, since="2026-11-13", until="2026-11-13")
    assert "планираме" in found and "morning brief" not in found
    by_word = await _recall(harness, words=["brief"])
    assert "the job: Run the morning brief" in by_word, "found by its words, and not as something he said"
    assert "YYYY-MM-DD" in await _recall(harness, since="last Friday")
    assert "what to look for" in await _recall(harness)


async def test_only_what_was_said_is_remembered_and_a_private_chat_never(harness: Harness):
    store = harness.core.store
    conv = await store.create_conversation(title="Chat")
    secret = await store.create_conversation(title="Private", incognito=True)
    await _say(harness, secret.id, "синхрофазотрон тайно", FRIDAY)
    await _say(harness, conv.id, "синхрофазотрон резултат", FRIDAY, Role.TOOL, name="web.search", tool_call_id="c")
    await _say(harness, conv.id, "синхрофазотрон контекст", FRIDAY, name="context")
    call = ToolCall(id="c", name="web.search", arguments={})
    await _say(harness, conv.id, "синхрофазотрон търся", FRIDAY, Role.ASSISTANT, tool_calls=[call])
    assert "Nothing said matches" in await _recall(harness, words=["синхрофазотрон"])
    await _say(harness, conv.id, "синхрофазотрон в срещата", FRIDAY, name="transcript")
    found = await _recall(harness, words=["синхрофазотрон"])
    assert "heard in a meeting: синхрофазотрон в срещата" in found
    assert "тайно" not in found and "контекст" not in found and "търся" not in found


async def test_the_question_itself_is_not_an_answer(harness: Harness):
    conv = await harness.core.store.create_conversation(title="Chat")
    await _say(harness, conv.id, "какво каза Гюзелев?", FRIDAY, run_id="run_now")
    token = current_run_id.set("run_now")
    try:
        assert "Nothing said matches" in await _recall(harness, words=["Гюзелев"])
    finally:
        current_run_id.reset(token)


async def test_a_deleted_conversation_is_forgotten_by_the_index(harness: Harness):
    db = harness.core.db
    conv = await harness.core.store.create_conversation(title="Chat")
    await _say(harness, conv.id, "Фибанк", FRIDAY)
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv.id,))
    assert await db.fetchall("SELECT id FROM said") == []
    assert await db.fetchall("SELECT rowid FROM said_fts WHERE said_fts MATCH 'Фибанк'") == []


async def test_migration_23_indexes_what_was_said_before_it(harness: Harness):
    db = harness.core.db
    conv = await harness.core.store.create_conversation(title="Chat")
    await _say(harness, conv.id, "Фибанк преди индекса", FRIDAY)
    for name in ("messages_said", "said_gone"):
        await db.execute(f"DROP TRIGGER {name}")
    await db.execute("DROP TABLE said_fts")
    await db.execute("DROP TABLE said")
    await db.execute("DELETE FROM schema_migrations WHERE version = 23")
    await db._migrate()
    assert "преди индекса" in await _recall(harness, words=["Фибанк"])


@pytest.mark.parametrize(("kind", "offered"), [(RunKind.CHAT, True), (RunKind.COLLAB, False)])
async def test_another_agent_is_not_offered_his_memory(harness: Harness, kind: RunKind, offered: bool):
    conv = await harness.core.store.create_conversation(title="Chat")
    harness.chat.push(FakeTurn(text="ok"))
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="hi", conversation_id=conv.id, kind=kind)
    await harness.wait_for(sub, "run.done")
    assert ("memory.recall" in {t.name for t in harness.chat.calls[0][1]}) is offered
