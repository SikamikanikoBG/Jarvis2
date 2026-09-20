"""Sessions: one chat of Jarvis's writing to another (features/sessions.py)."""

from __future__ import annotations

import asyncio

from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.features.sessions import SessionsTools, handle_of, handles_for
from jarvis_core.models.fake import FakeTurn
from jarvis_proto import Conversation, ConversationKind
from tests.conftest import Harness


def test_a_handle_is_the_title_slugged() -> None:
    assert handle_of("Домо — етажна собственост") == "домо-етажна-собственост"
    assert handle_of("HomeLab Monitor: release 0.14") == "homelab-monitor-release-0-14"
    assert handle_of("  ...  ") == "chat"
    assert len(handle_of("x" * 80)) == 40


def test_two_chats_of_the_same_name_are_told_apart_oldest_first() -> None:
    from datetime import UTC, datetime

    old = Conversation(id="c1", title="Домо", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    new = Conversation(id="c2", title="домо!", created_at=datetime(2026, 2, 1, tzinfo=UTC))
    handles = handles_for([new, old])
    assert handles == {"c1": "домо", "c2": "домо-2"}


def test_a_chat_imported_without_a_timezone_still_gets_a_handle() -> None:
    """Rows from the V1 import carry naive timestamps; sorting them beside today's aware ones
    raised TypeError and took the whole session list with it (found live, 2026-09-19)."""
    from datetime import UTC, datetime

    ancient = Conversation(id="c1", title="Архив", created_at=datetime(2024, 5, 1))
    today = Conversation(id="c2", title="Архив", created_at=datetime.now(UTC))
    assert handles_for([today, ancient]) == {"c1": "архив", "c2": "архив-2"}


async def _sessions(harness: Harness) -> SessionsTools:
    return harness.core.sessions


async def _call(tools: SessionsTools, name: str, **args: object) -> str:
    result = await tools.call(name, args, cancel=asyncio.Event(), idempotency_key="k", timeout_s=30)
    return result.text or result.error or ""


async def test_a_session_list_says_who_is_there_and_who_is_working(harness: Harness):
    tools = await _sessions(harness)
    domo = await harness.core.store.create_conversation(title="Домо")
    await harness.core.store.create_conversation(title="Пътуване до Рим")
    secret = await harness.core.store.create_conversation(title="Лично", incognito=True)

    listed = await _call(tools, "sessions.list")
    assert "@домо — Домо" in listed
    assert "@пътуване-до-рим" in listed
    # An incognito chat is not a session: not listed, not addressable, not readable.
    assert "Лично" not in listed and secret.id not in listed
    assert "idle" in listed

    # Standing inside a chat, it knows which one is itself.
    token = current_conversation_id.set(domo.id)
    try:
        assert "(this one)" in await _call(tools, "sessions.list")
    finally:
        current_conversation_id.reset(token)


async def test_saying_something_wakes_an_idle_session_and_brings_the_reply_back(harness: Harness):
    """The whole point: a chat with nothing going on is woken by another and answers it."""
    tools = await _sessions(harness)
    here = await harness.core.store.create_conversation(title="Тук")
    there = await harness.core.store.create_conversation(title="Домо")
    harness.chat.push(FakeTurn(text="Събранието е във вторник."))

    token = current_conversation_id.set(here.id)
    try:
        said = await _call(tools, "sessions.say", to="@домо", text="кога е събранието?")
    finally:
        current_conversation_id.reset(token)

    assert said == "@домо says: Събранието е във вторник."
    # It arrived as a message naming the session it came from, and it is in that chat for good.
    messages = await harness.core.store.list_messages(there.id)
    assert any(m.role.value == "user" and m.content == "[@тук] кога е събранието?" for m in messages)
    assert any(m.role.value == "assistant" and "вторник" in m.content for m in messages)


async def test_a_message_to_a_working_session_joins_its_run_instead_of_starting_another(harness: Harness):
    tools = await _sessions(harness)
    there = await harness.core.store.create_conversation(title="Домо")
    harness.chat.push(
        FakeTurn(text="one two three four five six seven eight", token_delay_s=0.05),
        FakeTurn(text="Добре, добавих го."),
    )
    run, _ = await harness.core.engine.create_run(text="направи нещо", conversation_id=there.id)
    async with asyncio.timeout(10):
        while not harness.chat.calls:
            await asyncio.sleep(0.01)

    said = await _call(tools, "sessions.say", to=there.id, text="и добави това")
    assert "Добре, добавих го." in said
    # One run, not two: the words joined what was already working there.
    runs = await harness.core.store.list_runs(there.id)
    assert [r.id for r in runs] == [run.id]


async def test_it_will_not_write_to_itself_or_to_a_session_already_in_the_chain(harness: Harness):
    tools = await _sessions(harness)
    here = await harness.core.store.create_conversation(title="Тук")
    there = await harness.core.store.create_conversation(title="Там")

    token = current_conversation_id.set(here.id)
    try:
        assert "that is this session" in await _call(tools, "sessions.say", to="@тук", text="ехо")
        # A run that arrived FROM @там may not write back to it: that is the loop.
        run_token = current_run_id.set("run_x")
        tools._remember("run_x", (there.id,))
        try:
            refused = await _call(tools, "sessions.say", to="@там", text="и обратно")
        finally:
            current_run_id.reset(run_token)
    finally:
        current_conversation_id.reset(token)
    assert "already in this chain" in refused


async def test_an_unknown_or_ambiguous_name_is_refused_with_the_candidates(harness: Harness):
    tools = await _sessions(harness)
    await harness.core.store.create_conversation(title="Домо събрание")
    await harness.core.store.create_conversation(title="Домо ремонт")

    assert "no session @няма" in await _call(tools, "sessions.say", to="@няма", text="ехо")
    both = await _call(tools, "sessions.say", to="@домо", text="ехо")
    assert "could be any of" in both and "@домо-събрание" in both and "@домо-ремонт" in both


async def test_reading_another_session_shows_its_last_words_and_wakes_nothing(harness: Harness):
    tools = await _sessions(harness)
    there = await harness.core.store.create_conversation(title="Домо", kind=ConversationKind.CHAT)
    harness.chat.push(FakeTurn(text="Готово."))
    await harness.core.engine.create_run(text="запиши: ключът е у Иван", conversation_id=there.id)
    sub = harness.subscribe(there.id)
    await harness.wait_for(sub, "run.done", timeout=10)

    read = await _call(tools, "sessions.read", of="@домо", limit=5)
    assert "Arsen: запиши: ключът е у Иван" in read
    assert "Jarvis: Готово." in read
    # Reading starts nothing.
    assert len(await harness.core.store.list_runs(there.id)) == 1


async def test_the_switch_in_settings_turns_the_whole_thing_off(harness: Harness):
    tools = await _sessions(harness)
    await harness.core.store.create_conversation(title="Домо")
    harness.enable(sessions=harness.core.settings.sessions.model_copy(update={"enabled": False}))
    assert "switched off" in await _call(tools, "sessions.say", to="@домо", text="ехо")
