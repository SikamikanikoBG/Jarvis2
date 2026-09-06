"""Slice 0 of docs/WAVE2.md: search, fork (edit-and-resend on an append-only history), pinned,
auto-title after the first exchange."""

from __future__ import annotations

import asyncio

import pytest

from jarvis_core.features.titles import clean_title
from jarvis_core.models.fake import FakeTurn
from jarvis_proto import Message
from tests.conftest import Harness


async def _seed(harness: Harness):  # type: ignore[no-untyped-def]
    store = harness.core.store
    a = await store.create_conversation(title="Budget planning")
    await store.add_message(Message.user("what is the Q4 budget status?", conversation_id=a.id))
    await store.add_message(Message.assistant("The Q4 budget is approved, 1.2M.", conversation_id=a.id))
    # Injected context is not user-visible text: it must never be a search hit.
    await store.add_message(Message.user("[Context] secret mango facts", conversation_id=a.id, name="context"))
    b = await store.create_conversation(title="Lunch")
    await store.add_message(Message.user("mango or sushi on Friday?", conversation_id=b.id))
    await store.add_message(Message.assistant("Sushi. The mango place is closed.", conversation_id=b.id))
    return a, b


async def test_search_titles_first_then_message_text_one_hit_per_conversation(harness: Harness):
    a, b = await _seed(harness)
    hits = await harness.core.store.search("budget")
    assert [h.conversation.id for h in hits] == [a.id]
    assert hits[0].matched == "title" and hits[0].snippet is None
    hits = await harness.core.store.search("mango")
    # Only the lunch chat: the "mango" in the other one lives in an injected context message.
    assert [h.conversation.id for h in hits] == [b.id]
    assert hits[0].matched == "message" and hits[0].message_id and "mango" in (hits[0].snippet or "").lower()
    assert await harness.core.store.search("   ") == []
    # LIKE metacharacters in the query are literal, not wildcards.
    assert await harness.core.store.search("%") == []
    assert await harness.core.store.search("_") == []


async def test_fork_copies_the_transcript_before_a_message_and_is_a_new_chat(harness: Harness):
    a, _ = await _seed(harness)
    msgs = await harness.core.store.list_messages(a.id)
    assistant_reply = msgs[1]
    fork = await harness.core.store.fork_conversation(a.id, up_to_message_id=assistant_reply.id)
    assert fork and fork.id != a.id and fork.title == "Budget planning (fork)" and fork.title_auto is False
    copied = await harness.core.store.list_messages(fork.id)
    assert [m.content for m in copied] == ["what is the Q4 budget status?"]
    assert copied[0].id != msgs[0].id and copied[0].run_id is None
    assert fork.message_count == 1 and fork.preview == "what is the Q4 budget status?"
    # The original is untouched.
    assert len(await harness.core.store.list_messages(a.id)) == 3
    # Whole transcript when no cut-off is given; unknown cut-off is an error, not a silent full copy.
    whole = await harness.core.store.fork_conversation(a.id, up_to_message_id=None)
    assert whole and len(await harness.core.store.list_messages(whole.id)) == 3
    with pytest.raises(ValueError):
        await harness.core.store.fork_conversation(a.id, up_to_message_id="msg_nope")
    assert await harness.core.store.fork_conversation("conv_nope", up_to_message_id=None) is None


async def test_pinned_and_title_auto_round_trip(harness: Harness):
    a, _ = await _seed(harness)
    assert a.pinned is False and a.title_auto is True
    conv = await harness.core.store.update_conversation(a.id, pinned=1)
    assert conv and conv.pinned is True
    conv = await harness.core.store.update_conversation(a.id, title="My budget", title_auto=0)
    assert conv and conv.title == "My budget" and conv.title_auto is False


def test_clean_title_strips_quotes_labels_and_length():
    assert clean_title('"Budget approval for Q4"') == "Budget approval for Q4"
    assert clean_title("Title: Sushi on Friday.") == "Sushi on Friday"
    assert clean_title("Заглавие: Бюджет Q4\nsecond line ignored") == "Бюджет Q4"
    assert clean_title("") is None and clean_title("   \n ") is None
    long = clean_title("x" * 80)
    assert long is not None and len(long) == 58 and long.endswith("…")


async def test_engine_titles_a_chat_once_after_the_first_exchange_and_respects_renames(harness: Harness):
    core = harness.core
    core.engine.set_titler(core.titler.title)
    # Turn 1: the reply. Turn 2: the classifier's title. Turn 3: a second reply (no title call).
    harness.chat.push(FakeTurn(text="Approved: 1.2M for Q4."), FakeTurn(text="Q4 budget approval"), FakeTurn(text="Yes."))
    conv = await core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await core.engine.create_run(text="is the Q4 budget approved?", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    for _ in range(50):
        got = await core.store.get_conversation(conv.id)
        if got and got.title == "Q4 budget approval":
            break
        await asyncio.sleep(0.05)
    got = await core.store.get_conversation(conv.id)
    assert got and got.title == "Q4 budget approval" and got.title_auto is True
    # Second exchange: the name stays and no model turn is spent on a title.
    await core.engine.create_run(text="fully?", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    await asyncio.sleep(0.1)
    got = await core.store.get_conversation(conv.id)
    assert got and got.title == "Q4 budget approval" and harness.chat.turns == []
    # A human rename wins for good: title_auto off, titler never called again.
    renamed = await core.store.update_conversation(conv.id, title="Budget", title_auto=0)
    assert renamed and renamed.title_auto is False
