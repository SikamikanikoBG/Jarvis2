"""The time where Arsen is, and his language, ride with every turn (review 2026-10-19).

At Munich airport Jarvis read a flight tracker's UTC times as local time and told him he had
"plenty of time" minutes before boarding: the model knew only the date in Sofia."""

from __future__ import annotations

from datetime import UTC, datetime

from jarvis_core.models.fake import FakeTurn
from jarvis_proto import Channel, RunKind
from tests.conftest import Harness


def _context(harness: Harness) -> str:
    sent = harness.chat.calls[0][0]
    return next(m.content for m in sent if m.name == "context")


async def test_a_turn_carries_the_time_on_his_device_and_in_the_home_timezone(harness: Harness):
    harness.core.context._clock = lambda: datetime(2026, 10, 19, 17, 41, tzinfo=UTC)
    harness.chat.push(FakeTurn(text="ok"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    run, _ = await harness.core.engine.create_run(
        text="is my flight on time?", conversation_id=conv.id, channel=Channel.VOICE, timezone="Europe/Berlin"
    )
    await harness.wait_for(sub, "run.done")
    stored = await harness.core.store.get_run(run.id)
    assert stored is not None and stored.timezone == "Europe/Berlin"
    text = _context(harness)
    assert "19:41" in text and "Europe/Berlin" in text  # where he is
    assert "20:41" in text and "Europe/Sofia" in text  # home, as the settings know it
    assert "UTC" in text  # sources' times are converted, not read as his
    assert harness.core.settings.language_hint in text


async def test_without_a_device_timezone_the_settings_timezone_is_his(harness: Harness):
    harness.core.context._clock = lambda: datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
    harness.chat.push(FakeTurn(text="ok"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="morning brief", conversation_id=conv.id, kind=RunKind.SCHEDULED)
    await harness.wait_for(sub, "run.done")
    text = _context(harness)
    assert "05:00" in text and "Europe/Sofia" in text
    assert harness.core.settings.language_hint in text


async def test_an_unknown_device_timezone_falls_back_to_the_settings(harness: Harness):
    harness.core.context._clock = lambda: datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
    harness.chat.push(FakeTurn(text="ok"))
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="hi", conversation_id=conv.id, timezone="Mars/Olympus")
    await harness.wait_for(sub, "run.done")
    text = _context(harness)
    assert "05:00" in text and "Mars" not in text
