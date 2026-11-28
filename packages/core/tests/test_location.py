"""Where Arsen is (features/location.py, 2026-11-28)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.features.location import Gazetteer, ago, owntracks_fix, place_at
from jarvis_core.models.base import reset_endpoint_semaphores
from jarvis_core.models.factory import current_run_kind
from jarvis_core.models.fake import FakeAdapter
from jarvis_proto import ConversationKind, DeviceLocation, Place, RoleName, Run, RunKind, new_id
from tests.conftest import Harness

# name, lat, lon, feature code, country, population - the GeoNames columns the gazetteer reads.
TOWNS = [
    ("Sofia", 42.69751, 23.32415, "PPLC", "BG", 1152556),
    ("Bankya", 42.70694, 23.14417, "PPL", "BG", 10000),
    ("Milan", 45.46427, 9.18951, "PPLA", "IT", 1371498),
    ("Brera", 45.47200, 9.18800, "PPLX", "IT", 18492),
]


def write_towns(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (name, lat, lon, code, cc, pop) in enumerate(TOWNS):
        cols = [str(i), name, name, "", str(lat), str(lon), "P", code, cc, "", "", "", "", "", str(pop), "", "", "", ""]
        rows.append("\t".join(cols))
    (folder / "cities1000.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


def fix(lat: float, lon: float, minutes_ago: float = 0, acc: float | None = 10, **kw) -> DeviceLocation:  # type: ignore[no-untyped-def]
    return DeviceLocation(
        lat=lat, lon=lon, accuracy_m=acc, taken_at=datetime.now(UTC) - timedelta(minutes=minutes_ago), **kw
    )


async def _run(core: Core, kind: RunKind = RunKind.CHAT, incognito: bool = False) -> Run:
    conv = await core.store.create_conversation(kind=ConversationKind.CHAT, incognito=incognito)
    return Run(id=new_id("run"), conversation_id=conv.id, kind=kind)


def test_the_gazetteer_names_the_town_and_the_city_a_district_belongs_to(tmp_path: Path):
    write_towns(tmp_path)
    g = Gazetteer(tmp_path, url="http://unused.invalid")
    asyncio.run(g.ready())
    assert g.describe(42.6800, 23.3200) == ("Sofia", "BG")
    assert g.describe(45.4725, 9.1882) == ("Brera, Milan", "IT")
    assert g.describe(0.0, 0.0) == (None, None)  # nowhere near a town


def test_place_at_ago_and_owntracks():
    home = Place(id="p1", name="Home", lat=42.68, lon=23.32, radius_m=150)
    assert place_at([home], 42.6805, 23.3205) == home  # ~68 m
    assert place_at([home], 42.6830, 23.3230) is None  # ~400 m
    assert place_at([home], 42.6816, 23.3200, accuracy_m=80) == home  # ~178 m, inside with the fix's slack
    now = datetime.now(UTC)
    assert ago(now - timedelta(seconds=30), now) == "just now"
    assert ago(now - timedelta(minutes=25), now) == "25 min ago"
    assert ago(now - timedelta(hours=9), now) == "9 h ago"
    got = owntracks_fix({"_type": "location", "lat": 45.47, "lon": 9.19, "acc": 12, "tst": 1795651200, "vel": 36})
    assert got is not None and got.source == "owntracks" and got.speed_mps == pytest.approx(10.0)
    assert owntracks_fix({"_type": "transition"}) is None


async def test_only_the_newest_fix_is_kept(harness: Harness):
    loc = harness.core.location
    assert await loc.update(fix(42.68, 23.32, minutes_ago=5))
    assert not await loc.update(fix(45.0, 9.0, minutes_ago=30))  # older than the one held
    assert not await loc.update(fix(42.70, 23.30, minutes_ago=4.9, acc=2000))  # coarse, same minute
    assert await loc.update(fix(42.70, 23.30, minutes_ago=1))
    held = await loc.store.last()
    assert held is not None and (held[0].lat, held[0].lon) == (42.70, 23.30)
    harness.core.settings.location.enabled = False
    assert not await loc.update(fix(1.0, 1.0))


async def test_the_turn_is_told_where_he_is_and_nobody_else_is(harness: Harness):
    core = harness.core
    write_towns(core.config.home / "geo")
    await core.location.store.add_place("Home", 42.6800, 23.3200, 150)
    await core.location.update(fix(45.4725, 9.1882, minutes_ago=20, speed_mps=0.5))

    blocks = dict(await core.context.turn_blocks(await _run(core), skill_names=[]))
    text = blocks["LocationBlock"]
    assert "In Brera, Milan, IT (±10 m, 20 min ago, from his phone)." in text
    assert "km from Home" in text and "Coordinates 45.47250, 9.18820" in text
    assert "somewhere else" not in text  # fresh

    scheduled = dict(await core.context.turn_blocks(await _run(core, RunKind.SCHEDULED), skill_names=[]))
    assert "LocationBlock" in scheduled
    for kind in (RunKind.COLLAB, RunKind.MAIL, RunKind.TRIAGE):
        assert "LocationBlock" not in dict(await core.context.turn_blocks(await _run(core, kind), skill_names=[]))
    assert "LocationBlock" not in dict(await core.context.turn_blocks(await _run(core, incognito=True), skill_names=[]))

    # City precision: the town and the place, never the coordinates.
    core.settings.location.precision = "city"
    await core.location.update(fix(42.6801, 23.3201, minutes_ago=0))
    text = dict(await core.context.turn_blocks(await _run(core), skill_names=[]))["LocationBlock"]
    assert (
        text.startswith("## Where Arsen is\nAt Home (Sofia, BG)")
        and "Coordinates" not in text
        and "km from Home" not in text
    )

    # An old fix is called old.
    await core.location.store.forget()
    await core.location.update(fix(42.6801, 23.3201, minutes_ago=60 * 9))
    text = dict(await core.context.turn_blocks(await _run(core), skill_names=[]))["LocationBlock"]
    assert "9 h ago" in text and "somewhere else" in text


async def test_places_tools_from_his_chat_but_not_from_a_run_nobody_watches(harness: Harness):
    core = harness.core
    await core.location.update(fix(42.68, 23.32))

    async def call(name: str, args: dict):  # type: ignore[no-untyped-def, type-arg]
        return await core.registry.call(name, args, cancel=asyncio.Event(), idempotency_key=f"{name}{args}")

    saved = await call("location.place_save", {"name": "Home"})
    assert saved.kind.value == "data"
    assert (await call("location.place_save", {"name": "home"})).kind.value == "error"  # same name
    assert "Home" in (await call("location.places", {})).text
    assert "At Home" in (await call("location.get", {})).text

    tokens = [current_run_id.set("run_x"), current_run_kind.set(RunKind.SCHEDULED), current_conversation_id.set(None)]
    try:
        refused = await call("location.place_save", {"name": "Gym", "lat": 42.6, "lon": 23.3})
    finally:
        current_conversation_id.reset(tokens[2])
        current_run_kind.reset(tokens[1])
        current_run_id.reset(tokens[0])
    assert refused.kind.value == "error"
    assert (await call("location.place_delete", {"place": "Home"})).kind.value == "data"
    assert await core.location.store.places() == []


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    write_towns(tmp_path / "geo")
    with TestClient(create_app(core.config, core=core)) as c:
        yield c


H = {"Authorization": "Bearer secret"}


def test_location_and_places_rest(client: TestClient):
    assert client.get("/api/location", headers=H).json()["fix"] is None
    assert client.post("/api/places", json={"name": "Home"}, headers=H).status_code == 422  # no fix yet
    now = datetime.now(UTC).isoformat()
    kept = client.post("/api/location", json={"lat": 42.68, "lon": 23.32, "accuracy_m": 8, "taken_at": now}, headers=H)
    assert kept.json() == {"kept": True}
    home = client.post("/api/places", json={"name": "Home"}, headers=H).json()
    assert home["lat"] == 42.68 and home["radius_m"] == 150
    view = client.get("/api/location", headers=H).json()
    assert view["place"] == "Home" and view["locality"] == "Sofia" and view["country"] == "BG"
    assert client.patch(f"/api/places/{home['id']}", json={"radius_m": 300}, headers=H).json()["radius_m"] == 300
    assert client.patch(f"/api/places/{home['id']}", json={"radius_m": 5}, headers=H).status_code == 422
    # OwnTracks: token in the URL, its own JSON, a list back.
    later = int(datetime.now(UTC).timestamp()) + 1
    ot = client.post(
        "/api/location/owntracks?token=secret",
        json={"_type": "location", "lat": 45.4725, "lon": 9.1882, "acc": 15, "tst": later},
    )
    assert ot.status_code == 200 and ot.json() == []
    assert client.get("/api/location", headers=H).json()["locality"] == "Brera, Milan"
    assert client.post("/api/location/owntracks", json={"_type": "location"}).status_code == 401
    assert client.delete(f"/api/places/{home['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/places/{home['id']}", headers=H).status_code == 404
    assert client.delete("/api/location", headers=H).status_code == 204
    assert client.get("/api/location", headers=H).json()["fix"] is None


# --- motion (2026-11-28: still or moving, and how) ------------------------------------------------


def test_bands_and_classify():
    from jarvis_core.features.location import band, classify

    assert [band(v / 3.6) for v in (0.5, 5, 10, 22, 90, 800)] == [
        "still",
        "walking",
        "running",
        "cycling",
        "vehicle",
        "flying",
    ]
    t0 = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
    a = DeviceLocation(lat=42.680, lon=23.320, accuracy_m=10, taken_at=t0)
    # 500 m in 6 min without a device speed = 5 km/h: walking, since the previous fix.
    b = DeviceLocation(lat=42.6845, lon=23.320, accuracy_m=10, taken_at=t0 + timedelta(minutes=6))
    m, since, speed = classify(b, a, None, None)
    assert m == "walking" and since == t0 and speed is not None and 4.5 < speed * 3.6 < 5.5
    # The same state again keeps its start.
    c = DeviceLocation(lat=42.689, lon=23.320, accuracy_m=10, taken_at=t0 + timedelta(minutes=12))
    assert classify(c, b, "walking", t0)[:2] == ("walking", t0)
    # Within the two fixes' accuracy = not moved.
    d = DeviceLocation(lat=42.68905, lon=23.320, accuracy_m=10, taken_at=t0 + timedelta(minutes=13))
    assert classify(d, c, "walking", t0)[0] == "still"
    # The device's own speed wins; a gap over 30 min says nothing.
    e = d.model_copy(update={"speed_mps": 25.0, "taken_at": t0 + timedelta(minutes=14)})
    assert classify(e, d, "still", t0)[0] == "vehicle"
    far = DeviceLocation(lat=45.0, lon=9.0, taken_at=t0 + timedelta(hours=5))
    assert classify(far, d, "still", t0)[0] == "unknown"


async def test_the_turn_hears_still_or_moving_and_since_when(harness: Harness):
    core = harness.core
    write_towns(core.config.home / "geo")
    await core.location.store.add_place("Home", 42.680, 23.320, 150)
    base = datetime.now(UTC) - timedelta(minutes=40)

    def at(m: float, lat: float = 42.680, **kw) -> DeviceLocation:  # type: ignore[no-untyped-def]
        return DeviceLocation(lat=lat, lon=23.320, accuracy_m=10, taken_at=base + timedelta(minutes=m), **kw)

    for m in (0, 10, 20):
        await core.location.update(at(m))
    text = core.location.describe(await core.location.view(), exact=False)
    assert text is not None and "At Home" in text and "Still for 20 min." in text

    await core.location.update(at(26, lat=42.6845))  # 500 m in 6 min
    await core.location.update(at(32, lat=42.6890))
    view = await core.location.view()
    assert view.motion == "walking" and view.motion_kmh is not None
    assert "Walking, about 5 km/h, for 12 min." in (core.location.describe(view, exact=False) or "")

    await core.location.update(at(33, lat=42.7000, speed_mps=16.0))
    assert "In a vehicle - car, bus or train, about 58 km/h" in (
        core.location.describe(await core.location.view(), exact=False) or ""
    )
