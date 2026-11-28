"""Turn-by-turn guidance on the way (features/navigation.py, 2026-11-28)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jarvis_core.features.navigation import Providers, instruction, spoken_distance, to_line_m
from jarvis_proto import DeviceLocation
from tests.conftest import Harness

# A walk: 300 m north along Street A, left, 200 m west along Street B to the cafe.
A = (42.6800, 23.3200)
CORNER = (42.6827, 23.3200)
END = (42.6827, 23.3176)


def _route(start: tuple[float, float]) -> dict[str, Any]:
    return {
        "distance": 500.0,
        "duration": 360.0,
        "geometry": {"coordinates": [[start[1], start[0]], [CORNER[1], CORNER[0]], [END[1], END[0]]]},
        "legs": [
            {
                "steps": [
                    {
                        "name": "Street A",
                        "distance": 300.0,
                        "maneuver": {"type": "depart", "bearing_after": 0, "location": [start[1], start[0]]},
                    },
                    {
                        "name": "Street B",
                        "distance": 200.0,
                        "maneuver": {"type": "turn", "modifier": "left", "location": [CORNER[1], CORNER[0]]},
                    },
                    {
                        "name": "",
                        "distance": 0.0,
                        "maneuver": {"type": "arrive", "modifier": "right", "location": [END[1], END[0]]},
                    },
                ]
            }
        ],
    }


class FakeProviders(Providers):
    def __init__(self) -> None:
        super().__init__()
        self.routes: list[tuple[float, float]] = []

    async def route(self, mode: str, a: tuple[float, float], b: tuple[float, float]) -> dict[str, Any]:
        self.routes.append(a)
        return _route(a)

    async def geocode(self, query: str, near: tuple[float, float] | None) -> tuple[str, float, float] | None:
        return ("Cafe Luna, Street B, Sofia", END[0], END[1]) if "luna" in query.lower() else None

    async def landmarks(self, points: list[tuple[float, float]]) -> list[str | None]:
        return ["the pharmacy Sopharma" for _ in points]


def at(lat: float, lon: float, sec: float = 0) -> DeviceLocation:
    return DeviceLocation(lat=lat, lon=lon, accuracy_m=5, taken_at=datetime.now(UTC) + timedelta(seconds=sec))


def test_straight_on_is_not_said():
    from jarvis_core.features.navigation import silent

    assert silent({"kind": "new name", "instruction": "continue straight on onto X"})
    assert not silent({"kind": "continue", "instruction": "turn left onto X"})
    assert not silent({"kind": "turn", "instruction": "turn right"})


def test_wording():
    assert (
        instruction({"name": "Graf Ignatiev", "maneuver": {"type": "turn", "modifier": "slight left"}}, "X")
        == "bear left onto Graf Ignatiev"
    )
    assert (
        instruction({"name": "", "maneuver": {"type": "roundabout", "exit": 2}}, "X")
        == "at the roundabout take the second exit"
    )
    assert (
        instruction({"name": "Vitosha", "maneuver": {"type": "depart", "bearing_after": 182}}, "X")
        == "head south on Vitosha"
    )
    assert instruction({"maneuver": {"type": "arrive", "modifier": "left"}}, "Cafe") == "Cafe is ahead, on your left"
    assert [spoken_distance(m) for m in (7, 44, 260, 1530)] == ["10 m", "40 m", "250 m", "1.5 km"]
    line = [[23.32, 42.68], [23.32, 42.69]]
    assert to_line_m(42.685, 23.32, line) < 1 and 75 < to_line_m(42.685, 23.321, line) < 90


async def test_a_walk_is_guided_turn_by_turn_and_ends_on_arrival(harness: Harness):
    core = harness.core
    nav = core.navigation
    nav.providers = FakeProviders()
    sub = harness.subscribe("none")
    await core.location.update(at(*A))

    overview = await nav.start("Cafe Luna", "walking", conversation_id=None)
    assert overview.startswith("On our way to Cafe Luna: 500 m, about 6 min walking. Head north on Street A.")
    assert "Then in 300 m, turn left onto Street B." in overview
    v = nav.view(at(*A))
    assert (
        v is not None and v.next_instruction == "Turn left onto Street B" and v.next_landmark == "the pharmacy Sopharma"
    )

    said: list[str] = []
    for i, lat in enumerate((42.6806, 42.6814, 42.6818, 42.6825, 42.6827)):  # walking north to the corner
        await core.location.update(at(lat, 23.3200, sec=10 * (i + 1)))
    for i, lon in enumerate((23.3195, 23.3188, 23.3180, 23.3177)):  # west along Street B
        await core.location.update(at(42.6827, lon, sec=60 + 10 * i))

    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if getattr(ev, "type", "") == "nav.prompt":
            said.append(ev.text)
    assert said[0].startswith("On our way to Cafe Luna")
    assert "In 100 m, turn left onto Street B, at the pharmacy Sopharma." in said
    assert "Turn left onto Street B, at the pharmacy Sopharma." in said
    assert any(s.startswith("In ") and "Cafe Luna is ahead, on your right" in s for s in said)
    assert said[-1] == "You have arrived at Cafe Luna."
    assert nav.session is None


async def test_off_the_route_twice_means_a_new_route_from_where_he_is(harness: Harness):
    core = harness.core
    nav = core.navigation
    fake = FakeProviders()
    nav.providers = fake
    await core.location.update(at(*A))
    await nav.start("Cafe Luna", "walking")
    await core.location.update(at(42.6806, 23.3215, sec=10))  # ~120 m east of the line
    assert len(fake.routes) == 1  # one fix off is not enough
    prompts = await nav.on_fix(at(42.6808, 23.3216, sec=20))
    assert len(fake.routes) == 2 and prompts and prompts[0][0] == "reroute"
    assert fake.routes[-1] == (42.6808, 23.3216)


async def test_guidance_survives_a_restart_and_stops(harness: Harness):
    core = harness.core
    core.navigation.providers = FakeProviders()
    await core.location.update(at(*A))
    await core.navigation.start("Cafe Luna", "cycling")
    core.navigation.session = None
    await core.navigation.load()
    assert core.navigation.session is not None and core.navigation.session.mode == "cycling"
    assert await core.navigation.stop() is True
    await core.navigation.load()
    assert core.navigation.session is None


async def test_no_fresh_location_no_guidance(harness: Harness):
    core = harness.core
    core.navigation.providers = FakeProviders()
    old = DeviceLocation(lat=A[0], lon=A[1], taken_at=datetime.now(UTC) - timedelta(hours=1))
    await core.location.update(old)
    try:
        await core.navigation.start("Cafe Luna")
    except ValueError as exc:
        assert "15 minutes" in str(exc)
    else:
        raise AssertionError("started on an hour-old fix")
