"""Meeting slots (issue #11): FreeBusy parsing, the intersection, and the backend over fake COM."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

import pytest
from fake_com import DirectoryPerson, FakeComError, World

from jarvis_host.outlook import (
    BusyBlock,
    FreeBusyPerson,
    OutlookBackend,
    OutlookError,
    OutlookService,
    ceil_to_grid,
    find_common_slots,
    parse_free_busy,
    work_windows,
)


class SofiaLike(tzinfo):
    """+03:00 until Sunday 2026-10-25 04:00 local, +02:00 after - Sofia's autumn change, without tzdata."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        assert dt is not None
        return timedelta(hours=3 if dt.replace(tzinfo=None) < datetime(2026, 10, 25, 4) else 2)

    def dst(self, dt: datetime | None) -> timedelta:
        return self.utcoffset(dt) - timedelta(hours=2)

    def tzname(self, dt: datetime | None) -> str:
        return "EEST" if self.dst(dt) else "EET"


TZ = SofiaLike()
BLOCKING = {"busy", "out_of_office", "tentative"}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)  # November: +02:00 throughout


def person(who: str, *blocks: tuple[datetime, datetime, str], until: datetime | None = None) -> FreeBusyPerson:
    return FreeBusyPerson(who, f"{who.lower()}@x.bg", [BusyBlock(*b) for b in blocks], until or at(30, 0))


def slots_of(people, start, end, *, duration=30, limit=3, spread=0, blocking=BLOCKING, wsh=9, weh=18):
    return find_common_slots(
        people,
        work_windows(start, end, wsh, weh, TZ),
        timedelta(minutes=duration),
        30,
        limit,
        timedelta(minutes=spread),
        blocking,
    )


# --- parsing ------------------------------------------------------------------------------------


def test_parse_merges_runs_maps_codes_and_reports_the_end_of_the_data():
    origin = at(2, 0)
    blocks, until = parse_free_busy("0012203490", origin, 30, TZ)
    assert [(b.start, b.end, b.status) for b in blocks] == [
        (at(2, 1), at(2, 1, 30), "tentative"),
        (at(2, 1, 30), at(2, 2, 30), "busy"),
        (at(2, 3), at(2, 3, 30), "out_of_office"),
        (at(2, 3, 30), at(2, 4), "working_elsewhere"),
        (at(2, 4), at(2, 4, 30), "busy"),  # an unknown code is never read as free
    ]
    assert until == at(2, 5)
    assert parse_free_busy("", origin, 30, TZ) == ([], origin)


def test_parse_steps_elapsed_time_across_a_dst_change_and_slots_keep_the_local_work_hours():
    # Asked on Friday 23.10 (+03:00); Sofia goes to +02:00 on Sunday 25.10, a 25-hour day.
    origin = datetime(2026, 10, 23, 0, 0, tzinfo=TZ)
    # Monday 26.10 09:00+02:00 is 82 elapsed hours after Friday 00:00+03:00 = character 164.
    codes = "0" * 164 + "22" + "0" * 40
    blocks, _ = parse_free_busy(codes, origin, 30, TZ)
    assert blocks[0].start == datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
    assert blocks[0].start.utcoffset() == timedelta(hours=2)
    p = FreeBusyPerson("Ana", "ana@x.bg", blocks, origin + timedelta(minutes=30 * len(codes)))
    slots, _ = find_common_slots(
        [p],
        work_windows(origin.replace(hour=8), datetime(2026, 10, 27, tzinfo=TZ), 9, 11, TZ),
        timedelta(minutes=60),
        30,
        5,
        timedelta(),
        BLOCKING,
    )
    assert [s["start"] for s in slots] == [
        "2026-10-23T09:00:00+03:00",
        "2026-10-23T10:00:00+03:00",
        "2026-10-26T10:00:00+02:00",  # Monday 09:00 local is busy - not shifted to 08:00 or 10:00
    ]


# --- the intersection ---------------------------------------------------------------------------


def test_common_slot_needs_everyone_free_and_tentative_blocks_only_when_asked():
    ana = person("Ana", (at(2, 9), at(2, 10), "busy"))
    bob = person("Bob", (at(2, 10), at(2, 11), "tentative"))
    slots, near = slots_of([ana, bob], at(2, 0), at(3, 0), duration=60, limit=1)
    assert [s["start"][11:16] for s in slots] == ["11:00"] and near == []
    slots, _ = slots_of([ana, bob], at(2, 0), at(3, 0), duration=60, limit=1, blocking={"busy", "out_of_office"})
    assert [s["start"][11:16] for s in slots] == ["10:00"]


def test_out_of_office_blocks_and_working_elsewhere_is_named_but_free():
    ana = person("Ana", (at(2, 0), at(3, 0), "out_of_office"))
    bob = person("Bob", (at(3, 0), at(4, 0), "working_elsewhere"))
    slots, _ = slots_of([ana, bob], at(2, 0), at(4, 0), limit=1)
    assert slots == [
        {"start": "2026-11-03T09:00:00+02:00", "end": "2026-11-03T09:30:00+02:00", "working_elsewhere": ["Bob"]}
    ]


def test_nobody_free_gives_near_misses_with_who_blocks_fewest_first():
    ana = person("Ana", (at(2, 9), at(2, 18), "busy"))
    bob = person("Bob", (at(2, 9), at(2, 12), "out_of_office"))
    slots, near = slots_of([ana, bob], at(2, 0), at(3, 0), limit=2, spread=60)
    assert slots == []
    assert [(n["start"][11:16], [b["who"] for b in n["blocked_by"]]) for n in near] == [
        ("12:00", ["Ana"]),
        ("13:00", ["Ana"]),
    ]
    assert near[0]["blocked_by"] == [{"who": "Ana", "status": "busy"}]


def test_limit_and_spread_and_no_overlapping_proposals():
    slots, _ = slots_of([person("Ana")], at(2, 0), at(3, 0), duration=60, limit=3, spread=120)
    assert [s["start"][11:16] for s in slots] == ["09:00", "11:00", "13:00"]
    slots, _ = slots_of([person("Ana")], at(2, 0), at(3, 0), duration=60, limit=2, spread=0)
    assert [s["start"][11:16] for s in slots] == ["09:00", "10:00"]  # spread is never below the duration


def test_weekends_and_out_of_hours_are_skipped():
    # Saturday 7.11 morning to Monday 9.11 noon: only Monday 09-12 counts.
    windows = work_windows(at(7, 6), at(9, 12), 9, 18, TZ)
    assert windows == [(at(9, 9), at(9, 12))]
    slots, _ = slots_of([person("Ana")], at(7, 6), at(9, 12), limit=10, spread=0)
    assert slots[0]["start"] == "2026-11-09T09:00:00+02:00" and slots[-1]["end"] == "2026-11-09T12:00:00+02:00"


def test_first_day_starts_at_the_next_grid_point_after_start():
    assert ceil_to_grid(at(2, 9, 10), 30) == at(2, 9, 30)
    assert ceil_to_grid(at(2, 9, 30), 30) == at(2, 9, 30)
    assert ceil_to_grid(at(2, 9, 0).replace(second=5), 30) == at(2, 9, 30)
    assert ceil_to_grid(at(2, 9, 50), 15) == at(2, 10, 0)
    slots, _ = slots_of([person("Ana")], at(2, 9, 10), at(3, 0), limit=1)
    assert slots[0]["start"] == "2026-11-02T09:30:00+02:00"


def test_times_past_the_published_data_are_not_offered():
    ana = person("Ana", until=at(2, 10))
    slots, near = slots_of([ana], at(2, 0), at(3, 0), limit=3, spread=0)
    assert [s["start"][11:16] for s in slots] == ["09:00", "09:30"]
    assert near[0]["blocked_by"] == [{"who": "Ana", "status": "no_data_this_far_ahead"}]


# --- the backend over fake COM ------------------------------------------------------------------


def half_hours(day_codes: dict[int, str], days: int = 7) -> str:
    """A 30-min FreeBusy string from Monday 00:00: {day index: 48 codes} (the rest free)."""
    return "".join(day_codes.get(d, "0" * 48) for d in range(days))


def busy_from(h0: float, h1: float, code: str = "2") -> str:
    a, b = int(h0 * 2), int(h1 * 2)
    return "0" * a + code * (b - a) + "0" * (48 - b)


@pytest.fixture
def world() -> World:
    w = World()
    w.ns.current_user_name = "Arsen Apostolov"
    w.ns.directory = [
        DirectoryPerson("Arsen Apostolov", "aapostolov@postbank.bg", half_hours({0: busy_from(9, 10)})),
        DirectoryPerson("Vesela Chuleva", "vchuleva@postbank.bg", half_hours({0: busy_from(10, 10.5, "1")})),
        DirectoryPerson("Petya Karova", "pkarova@postbank.bg", half_hours({0: busy_from(12, 13.5)})),
        DirectoryPerson("Nikolay Petrov", "nipetrov@postbank.bg", half_hours({})),
        DirectoryPerson("Nikola Petrov", "npetrov@postbank.bg", half_hours({})),
        DirectoryPerson("Private Person", "private@postbank.bg", FakeComError(-2147467259, "The operation failed.")),
    ]
    return w


@pytest.fixture
def backend(world: World) -> OutlookBackend:
    return OutlookBackend(world.dispatch)


def test_backend_intersects_everyone_including_me_and_reports_who_has_no_data(backend: OutlookBackend, world: World):
    res = backend.calendar_free_busy(
        "VChuleva@postbank.bg, Petya Karova, Petrov, Nobody Here, private@postbank.bg, aapostolov@postbank.bg",
        "2026-11-02",
        1,
        30,
        9,
        18,
        3,
        True,
        True,
        60,
    )
    by_who = {p["who"]: p for p in res["people"]}
    assert by_who["Arsen Apostolov"]["me"] is True and by_who["Arsen Apostolov"]["address"] == "aapostolov@postbank.bg"
    assert by_who["Vesela Chuleva"]["busy"] == [
        {"start": iso("2026-11-02T10:00"), "end": iso("2026-11-02T10:30"), "status": "tentative"}
    ]
    assert by_who["Petrov"]["no_data"].startswith("not resolved")  # two Petrovs: ask, never guess
    assert by_who["Petrov"]["nearest_in_address_book"]["address"].endswith("@postbank.bg")
    assert by_who["Nobody Here"]["no_data"].startswith("not resolved")
    assert "free/busy not available" in by_who["Private Person"]["no_data"]
    assert sum(1 for p in res["people"] if p.get("address") == "aapostolov@postbank.bg") == 1  # me once
    # 09 me busy, 10:00 Chuleva tentative → 10:30 is the first common slot; 12-13:30 Karova.
    assert [s["start"][11:16] for s in res["slots"]] == ["10:30", "11:30", "13:30"]
    assert res["near_misses"] == []
    assert res["note"] == "slots ignore people without free/busy data: Petrov, Nobody Here, Private Person"
    # FreeBusy is asked by local noon of the start date, in complete format.
    name, asked, minutes, complete = world.ns.free_busy_calls[0]
    assert (name, asked, minutes, complete) == ("Arsen Apostolov", datetime(2026, 11, 2, 12), 30, True)


def test_backend_uses_quarter_hours_for_a_45_minute_meeting_and_respects_include_me(
    backend: OutlookBackend, world: World
):
    res = backend.calendar_free_busy("Petya Karova", "2026-11-02T11:00", 1, 45, 9, 18, 2, False, True, 0)
    assert [p["who"] for p in res["people"]] == ["Petya Karova"]
    assert {c[2] for c in world.ns.free_busy_calls} == {15}
    assert [(s["start"][11:16], s["end"][11:16]) for s in res["slots"]] == [("11:00", "11:45"), ("13:30", "14:15")]


def test_backend_with_nobody_resolvable_returns_no_slots_and_says_why(backend: OutlookBackend):
    res = backend.calendar_free_busy("Nobody Here", "2026-11-02", 1, 30, 9, 18, 3, False, True, 120)
    assert res["slots"] == [] and res["near_misses"] == [] and "Nobody Here" in res["note"]
    with pytest.raises(OutlookError, match="no attendees"):
        backend.calendar_free_busy("", "2026-11-02", 1, 30, 9, 18, 3, False, True, 120)


def test_service_marks_free_busy_read_only_with_its_own_deadline():
    assert "calendar_free_busy" in OutlookService.READ_ONLY
    assert OutlookService.TIMEOUTS["calendar_free_busy"] >= 60


def iso(local: str) -> str:
    return datetime.fromisoformat(local).astimezone().isoformat(timespec="seconds")
