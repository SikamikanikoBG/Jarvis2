"""Episodes for the days before episodes existed (features/backfill.py, memory-track PROPOSAL §6.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis_core.features.backfill import MAX_TRIES, QUIET_S, in_hours
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import AssistantSettings, ConversationKind, RoleName
from tests.conftest import Harness


def _answer(title: str, what: str, *, continues: bool = False, quiet: bool = False) -> FakeTurn:
    episode = {"continues": continues, "title": title, "what": what, "people": [], "places": [], "importance": 3}
    return FakeTurn(text=json.dumps({"summary": "-", "waiting": "", "quiet": quiet, "episode": episode}))


async def _turns(harness: Harness, conversation_id: str, texts: list[str], start: datetime) -> list[str]:
    """Runs of ``conversation_id``, one per text, moved back to ``start`` and the minutes after it."""
    ids: list[str] = []
    for i, text in enumerate(texts):
        harness.chat.push(FakeTurn(text=f"ok {i}"))
        sub = harness.subscribe(conversation_id)
        run, _ = await harness.core.engine.create_run(text=text, conversation_id=conversation_id)
        await harness.wait_for(sub, "run.done")
        at = start + timedelta(minutes=10 * i)
        await harness.core.db.execute(
            "UPDATE runs SET created_at = ?, finished_at = ? WHERE id = ?",
            (at.isoformat(), (at + timedelta(minutes=1)).isoformat(), run.id),
        )
        ids.append(run.id)
    return ids


def _writer(harness: Harness) -> FakeAdapter:
    harness.enable(assistant=AssistantSettings(journal=True))
    writer = FakeAdapter()
    harness.core.adapters.fakes[RoleName.CLASSIFIER] = writer
    return writer


def _local(harness: Harness, day: int, hour: int, minute: int = 0) -> datetime:
    tz = ZoneInfo(harness.core.settings.timezone)
    return datetime(2026, 11, day, hour, minute, tzinfo=tz).astimezone(UTC)


async def test_past_days_become_episodes_oldest_first_and_their_digests_stay(harness: Harness):
    core = harness.core
    work = await core.store.create_conversation(title="Работа")
    home = await core.store.create_conversation(title="Вкъщи")
    texts = [f"A-Flow, част {i}" for i in range(7)]
    runs = await _turns(harness, work.id, texts, _local(harness, 10, 9))
    await _turns(harness, home.id, ["гумите са за понеделник"], _local(harness, 9, 18))
    writer = _writer(harness)
    await core.db.execute(
        "INSERT INTO conversation_digests(conversation_id, day, summary, waiting, quiet, run_id, updated_at)"
        " VALUES (?, '2026-11-10', 'Written that day.', '', 0, NULL, '2026-11-10T20:00:00+00:00')",
        (work.id,),
    )
    writer.push(
        _answer("Winter tyres", "Booked for Monday."),
        _answer("A-Flow with Gyuzelev", "He wants a demo."),
        _answer("A-Flow with Gyuzelev", "He wants a demo on Friday.", continues=True),
    )

    assert await core.backfill.work(now=_local(harness, 12, 2, 30)) == 3

    # The older day first; then the talk of the 10th in two batches, which stay one episode.
    prompts = [c[0][0].content for c in writer.calls]
    assert "гумите" in prompts[0] and "част 0" in prompts[1] and "част 4" in prompts[1]
    assert "част 5" in prompts[2] and "част 4" not in prompts[2] and "He wants a demo." in prompts[2]
    (talk,) = await core.journal.episodes.of_day(work.id, "2026-11-10")
    assert (talk.what, talk.first_run_id, talk.last_run_id) == ("He wants a demo on Friday.", runs[0], runs[-1])
    assert [e.title for e in await core.journal.episodes.of_day(home.id, "2026-11-09")] == ["Winter tyres"]
    # The day's digest is what was written that day.
    (digest,) = await core.db.fetchall("SELECT summary FROM conversation_digests")
    assert digest["summary"] == "Written that day."
    # Done is done: the next night asks nothing.
    assert await core.backfill.work(now=_local(harness, 13, 2, 30)) == 0
    assert len(writer.calls) == 3


async def test_it_works_only_at_night_and_never_while_he_is_talking(harness: Harness):
    core = harness.core
    conv = await core.store.create_conversation(title="Чат")
    await _turns(harness, conv.id, ["нещо"], _local(harness, 11, 0, 50))
    _writer(harness)
    backfill = core.backfill
    assert not await backfill.may_work(_local(harness, 11, 12))  # daytime
    assert not await backfill.may_work(_local(harness, 11, 1, 10))  # 20 minutes after his last chat
    assert await backfill.may_work(_local(harness, 11, 0, 50) + timedelta(seconds=QUIET_S))
    # A chat of his that is still running holds it back, however old.
    await core.db.execute("UPDATE runs SET status = 'running'")
    assert not await backfill.may_work(_local(harness, 11, 3))
    await core.db.execute("UPDATE runs SET status = 'done'")
    harness.enable(assistant=AssistantSettings(journal=True, backfill=False))
    assert not await backfill.may_work(_local(harness, 11, 3))
    assert in_hours(23, (22, 5)) and in_hours(4, (22, 5)) and not in_hours(5, (22, 5))


async def test_what_the_live_journal_wrote_up_is_not_asked_again(harness: Harness):
    core = harness.core
    conv = await core.store.create_conversation(title="Работа", kind=ConversationKind.SCHEDULED)
    runs = await _turns(harness, conv.id, ["преди", "след"], _local(harness, 10, 9))
    writer = _writer(harness)
    second = await core.store.get_run(runs[1])
    assert second is not None and second.finished_at is not None
    await core.db.execute(
        "INSERT INTO episodes(conversation_id, day, started_at, ended_at, title, what, first_run_id, last_run_id,"
        " updated_at) VALUES (?, '2026-11-10', ?, ?, 'Live', 'Written live.', ?, ?, ?)",
        (conv.id, second.created_at.isoformat(), second.finished_at.isoformat(), runs[1], runs[1], "x"),
    )
    writer.push(_answer("Before", "The run before."))
    assert await core.backfill.work(now=_local(harness, 12, 2)) == 1
    assert "преди" in writer.calls[0][0][0].content and "след" not in writer.calls[0][0][0].content


async def test_an_answer_it_cannot_read_is_asked_again_then_passed_over(harness: Harness):
    core = harness.core
    conv = await core.store.create_conversation(title="Чат")
    await _turns(harness, conv.id, ["едно"], _local(harness, 10, 9))
    writer = _writer(harness)
    night = _local(harness, 12, 2)
    for _ in range(MAX_TRIES):
        writer.push(FakeTurn(text="not JSON"))
        assert await core.backfill.work(now=night) == 1
        # Paused after a failure: the server may be down.
        assert await core.backfill.work(now=night) == 0
        core.backfill._paused_until = None
    assert await core.db.fetchall("SELECT * FROM episodes") == []
    (mark,) = await core.db.fetchall("SELECT done FROM episode_backfill")
    assert mark["done"] == 1
    assert await core.backfill.work(now=night) == 0
