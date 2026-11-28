"""Where Arsen is (2026-11-28: "so he can look after me more personally").

His phone sends its position with each message while he lets that device share it; OwnTracks may
send it in the background. The core keeps only the LAST fix - one row, overwritten, never a trail
- and makes three things of it for every turn, a scheduled one too (LocationBlock):

- the town, from an offline gazetteer (GeoNames cities1000, fetched once into the data folder): the
  coordinates never go to a geocoding service;
- the place he named, when the fix is inside one (Home, Office, Gym) - and how far from Home;
- how old the fix is, so a morning brief knows he is in Milan and also that it learned that at
  23:10 last night.

Nothing of it reaches a run that answers someone else (collab, mail, triage) or a private chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import DeviceLocation, LocationView, Place, Run, RunKind, ToolResult, new_id
from jarvis_proto.settings import LocationSettings

if TYPE_CHECKING:
    from jarvis_core.app import Core
    from jarvis_core.db.store import Store

log = logging.getLogger(__name__)

GAZETTEER_URL = "https://download.geonames.org/export/dump/cities1000.zip"
GAZETTEER_FILE = "cities1000.txt"
# A failed download is tried again after this long, not on every turn.
RETRY_AFTER_S = 3600
# The nearest named settlement further than this is no answer: he is between towns.
NEAR_KM = 25.0
# Run kinds that work for Arsen himself. A collab run answers someone else, a mail or triage run
# writes to other people: none of them is told where he is.
FOR_HIM = {RunKind.CHAT, RunKind.SCHEDULED, RunKind.TASK}
# A fix this much less precise than the one held does not replace a fresher-looking one taken
# within the same minute (a coarse network fix right after a GPS one).
_SAME_MINUTE_S = 60


def _now() -> datetime:
    return datetime.now(UTC)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ago(then: datetime, now: datetime) -> str:
    s = max(0, int((now - then).total_seconds()))
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 48 * 3600:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min ago" if h < 6 and m else f"{h} h ago"
    return f"{s // 86400} days ago"


# --- the gazetteer ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Town:
    name: str
    lat: float
    lon: float
    code: str  # GeoNames feature code: PPLC capital, PPLA seat, PPL town, PPLX a part of a city
    country: str
    population: int


class Gazetteer:
    """Nearest named settlement, offline. 170k places in 1-degree cells, loaded once."""

    def __init__(self, folder: Path, url: str = GAZETTEER_URL) -> None:
        self.folder = folder
        self.url = url
        self._cells: dict[tuple[int, int], list[Town]] | None = None
        self._failed_at = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[bool] | None = None

    @property
    def path(self) -> Path:
        return self.folder / GAZETTEER_FILE

    async def ready(self) -> bool:
        if self._cells is not None:
            return True
        if time.monotonic() - self._failed_at < RETRY_AFTER_S and self._failed_at:
            return False
        async with self._lock:
            if self._cells is not None:
                return True
            try:
                if not self.path.exists():
                    await self._download()
                self._cells = await asyncio.to_thread(self._load, self.path)
                log.info("gazetteer: %d cells loaded", len(self._cells))
            except Exception:
                log.exception("gazetteer unavailable; towns are left out until it loads")
                self._failed_at = time.monotonic()
                return False
        return True

    async def soon(self) -> bool:
        """Loaded now, or loading from disk now; a download goes to the background and this turn
        goes without the town rather than wait for 11 MB."""
        if self._cells is not None:
            return True
        if self.path.exists():
            return await self.ready()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.ready(), name="gazetteer")
        return False

    async def _download(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(self.url)
            response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            data = z.read(GAZETTEER_FILE)
        tmp = self.path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(self.path)

    @staticmethod
    def _load(path: Path) -> dict[tuple[int, int], list[Town]]:
        cells: dict[tuple[int, int], list[Town]] = {}
        with path.open(encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 15:
                    continue
                try:
                    town = Town(c[1], float(c[4]), float(c[5]), c[7], c[8], int(c[14] or 0))
                except ValueError:
                    continue
                cells.setdefault((math.floor(town.lat), math.floor(town.lon)), []).append(town)
        return cells

    def nearest(
        self, lat: float, lon: float, *, parts: bool = True, min_population: int = 0
    ) -> tuple[Town, float] | None:
        if self._cells is None:
            return None
        best: tuple[Town, float] | None = None
        cy, cx = math.floor(lat), math.floor(lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for t in self._cells.get((cy + dy, (cx + dx + 180) % 360 - 180), ()):
                    if (not parts and t.code == "PPLX") or t.population < min_population:
                        continue
                    d = haversine_km(lat, lon, t.lat, t.lon)
                    if best is None or d < best[1]:
                        best = (t, d)
        return best if best and best[1] <= NEAR_KM else None

    def describe(self, lat: float, lon: float) -> tuple[str | None, str | None]:
        """("Brera, Milan", "IT"), ("Sofia", "BG"), or (None, None) between towns or unloaded."""
        near = self.nearest(lat, lon)
        if near is None:
            return None, None
        town, _d = near
        if town.code != "PPLX":
            return town.name, town.country
        # A part of a city: name the city it belongs to as well.
        city = self.nearest(lat, lon, parts=False, min_population=10_000)
        if city is None or city[0].name == town.name:
            return town.name, town.country
        return f"{town.name}, {city[0].name}", town.country


# --- the store ----------------------------------------------------------------------------------


class LocationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def last(self) -> tuple[DeviceLocation, datetime] | None:
        row = await self.db.fetchone("SELECT * FROM location_last WHERE id = 1")
        if row is None:
            return None
        fix = DeviceLocation(
            lat=row["lat"],
            lon=row["lon"],
            accuracy_m=row["accuracy_m"],
            speed_mps=row["speed_mps"],
            source=row["source"],
            taken_at=datetime.fromisoformat(row["taken_at"]),
        )
        return fix, datetime.fromisoformat(row["received_at"])

    async def put(self, fix: DeviceLocation, received_at: datetime) -> None:
        await self.db.execute(
            "INSERT INTO location_last(id, lat, lon, accuracy_m, speed_mps, source, taken_at, received_at)"
            " VALUES (1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon,"
            " accuracy_m=excluded.accuracy_m, speed_mps=excluded.speed_mps, source=excluded.source,"
            " taken_at=excluded.taken_at, received_at=excluded.received_at",
            (
                fix.lat,
                fix.lon,
                fix.accuracy_m,
                fix.speed_mps,
                fix.source,
                fix.taken_at.astimezone(UTC).isoformat(),
                received_at.isoformat(),
            ),
        )

    async def forget(self) -> None:
        await self.db.execute("DELETE FROM location_last")

    async def places(self) -> list[Place]:
        rows = await self.db.fetchall("SELECT * FROM places ORDER BY created_at")
        return [
            Place(
                id=r["id"],
                name=r["name"],
                lat=r["lat"],
                lon=r["lon"],
                radius_m=r["radius_m"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def add_place(self, name: str, lat: float, lon: float, radius_m: float = 150) -> Place:
        name = name.strip()
        if not name:
            raise ValueError("a place needs a name")
        if not 20 <= radius_m <= 50_000:
            raise ValueError("radius_m must be between 20 and 50000")
        if any(p.name.casefold() == name.casefold() for p in await self.places()):
            raise ValueError(f"there is already a place named {name!r}; change or delete that one")
        place = Place(id=new_id("plc"), name=name, lat=lat, lon=lon, radius_m=radius_m)
        await self.db.execute(
            "INSERT INTO places(id, name, lat, lon, radius_m, created_at) VALUES (?,?,?,?,?,?)",
            (place.id, place.name, lat, lon, radius_m, place.created_at.isoformat()),
        )
        return place

    async def update_place(self, place_id: str, **fields: Any) -> Place | None:
        current = next((p for p in await self.places() if p.id == place_id), None)
        if current is None:
            return None
        data = current.model_dump()
        data.update({k: v for k, v in fields.items() if v is not None and k in ("name", "lat", "lon", "radius_m")})
        if not str(data["name"]).strip():
            raise ValueError("a place needs a name")
        if not 20 <= float(data["radius_m"]) <= 50_000:
            raise ValueError("radius_m must be between 20 and 50000")
        await self.db.execute(
            "UPDATE places SET name=?, lat=?, lon=?, radius_m=? WHERE id=?",
            (str(data["name"]).strip(), data["lat"], data["lon"], data["radius_m"], place_id),
        )
        return next((p for p in await self.places() if p.id == place_id), None)

    async def delete_place(self, place_id: str) -> bool:
        before = len(await self.places())
        await self.db.execute("DELETE FROM places WHERE id = ?", (place_id,))
        return len(await self.places()) < before


def place_at(places: list[Place], lat: float, lon: float, accuracy_m: float | None = None) -> Place | None:
    """The named place the fix falls in (the closest, if several overlap). A fix's own radius
    counts in its favour up to 100 m: a phone indoors reports ±60 m from the middle of its flat."""
    slack = min(accuracy_m or 0, 100)
    inside = [
        (haversine_km(lat, lon, p.lat, p.lon) * 1000, p)
        for p in places
        if haversine_km(lat, lon, p.lat, p.lon) * 1000 <= p.radius_m + slack
    ]
    return min(inside, key=lambda x: x[0])[1] if inside else None


# --- the service --------------------------------------------------------------------------------


class LocationService:
    def __init__(
        self,
        db: Database,
        folder: Path,
        settings: Callable[[], LocationSettings],
        gazetteer: Gazetteer | None = None,
    ) -> None:
        self.store = LocationStore(db)
        self.gazetteer = gazetteer or Gazetteer(folder)
        self._settings = settings

    @property
    def settings(self) -> LocationSettings:
        return self._settings()

    async def update(self, fix: DeviceLocation) -> bool:
        """Take a fix from a device; False when it was not kept (off, or older than the one held)."""
        if not self.settings.enabled:
            return False
        now = _now()
        if fix.taken_at.tzinfo is None:
            fix = fix.model_copy(update={"taken_at": fix.taken_at.replace(tzinfo=UTC)})
        # A device clock ahead of ours must not pin a fix in the future for ever.
        if fix.taken_at > now:
            fix = fix.model_copy(update={"taken_at": now})
        held = await self.store.last()
        if held is not None:
            old, _received = held
            if fix.taken_at < old.taken_at:
                return False
            same_minute = (fix.taken_at - old.taken_at).total_seconds() < _SAME_MINUTE_S
            coarser = (fix.accuracy_m or 0) > 4 * max(old.accuracy_m or 0, 10)
            if same_minute and coarser:
                return False
        await self.store.put(fix, now)
        return True

    async def view(self) -> LocationView:
        held = await self.store.last()
        if held is None:
            return LocationView()
        fix, received = held
        await self.gazetteer.soon()
        locality, country = self.gazetteer.describe(fix.lat, fix.lon)
        places = await self.store.places()
        place = place_at(places, fix.lat, fix.lon, fix.accuracy_m)
        home = next((p for p in places if p.name.casefold() == "home"), None)
        return LocationView(
            fix=fix,
            received_at=received,
            locality=locality,
            country=country,
            place=place.name if place else None,
            from_home_km=round(haversine_km(fix.lat, fix.lon, home.lat, home.lon), 1) if home else None,
        )

    def describe(self, view: LocationView, *, exact: bool, now: datetime | None = None) -> str | None:
        """One paragraph for a turn: where, how sure, how old - and the coordinates when allowed."""
        fix = view.fix
        if fix is None:
            return None
        now = now or _now()
        where = ", ".join(x for x in (view.locality, view.country) if x) or None
        if view.place:
            head = f"At {view.place}" + (f" ({where})" if where else "")
        elif where:
            head = f"In {where}"
        else:
            head = "Between towns"
        bits = [ago(fix.taken_at, now)]
        if fix.accuracy_m is not None:
            bits.insert(0, f"±{round(fix.accuracy_m)} m")
        bits.append({"browser": "from his phone", "owntracks": "from OwnTracks", "manual": "set by hand"}[fix.source])
        line = f"{head} ({', '.join(bits)})."
        if view.from_home_km is not None and (view.place or "").casefold() != "home" and view.from_home_km >= 1:
            line += f" {view.from_home_km:,.0f} km from Home."
        if fix.speed_mps is not None and fix.speed_mps >= 2:
            line += f" Moving at about {round(fix.speed_mps * 3.6)} km/h when it was taken."
        age_h = (now - fix.taken_at).total_seconds() / 3600
        if age_h >= self.settings.stale_after_h:
            line += " That is the last fix he sent; he may well be somewhere else by now."
        if exact:
            line += (
                f"\nCoordinates {fix.lat:.5f}, {fix.lon:.5f} - for nearby searches, routes, travel times and"
                " weather. Use them for him; never put them in anything sent to someone else unless he asks."
            )
        return line


class LocationBlock:
    """Per-turn context: where he is, for the runs that work for him."""

    def __init__(self, service: LocationService, store: Store) -> None:
        self._service = service
        self._store = store

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        s = self._service.settings
        if not s.enabled or run.kind not in FOR_HIM:
            return None
        conv = await self._store.get_conversation(run.conversation_id)
        if conv is not None and conv.incognito:
            return None
        text = self._service.describe(await self._service.view(), exact=s.precision == "exact")
        if text is None:
            return None
        return (
            "## Where Arsen is\n"
            + text
            + "\nLet it shape what you say when it matters - local time, what is near, the trip he is on,"
            " whether he is home - and do not recite it when it does not."
        )


# --- tools --------------------------------------------------------------------------------------


class _SaveArgs(BaseModel):
    name: str = Field(description="What he calls it: 'Home', 'Office', 'Gym', 'Mum's'")
    radius_m: float = Field(default=150, description="How far from its middle still counts as being there, in metres")
    lat: float | None = Field(default=None, description="Leave out to use where he is now")
    lon: float | None = Field(default=None, description="Leave out to use where he is now")


class _PlaceArgs(BaseModel):
    place: str = Field(description="The place's id or its name")


class LocationTools(BuiltinProvider):
    """``location.*``: where he is now and the places he named. Saving or deleting a place is for
    his own chat - a run nobody is watching may read, not redraw his map."""

    name = "location"

    def __init__(self, service: LocationService, core: Core | None = None) -> None:
        self.service = service
        self._core = core
        super().__init__()

    async def _by_run(self) -> bool:
        if self._core is None:
            return False
        from jarvis_core.features.admin import who_now

        return (await who_now(self._core)).author is not None

    async def _find(self, ref: str) -> Place | None:
        places = await self.service.store.places()
        return next((p for p in places if p.id == ref), None) or next(
            (p for p in places if p.name.casefold() == ref.strip().casefold()), None
        )

    @tool(
        "location.get",
        description="Where Arsen is now: the last fix from his phone, the town, the named place, how old it is, distance from Home.",
        read_only=True,
        idempotent=True,
    )
    async def _get(self) -> ToolResult:
        if not self.service.settings.enabled:
            return ToolResult.empty("Location is switched off in Settings.")
        view = await self.service.view()
        text = self.service.describe(view, exact=self.service.settings.precision == "exact")
        if text is None:
            return ToolResult.empty("No location yet: none of his devices has shared one.")
        return ToolResult.data(text)

    @tool(
        "location.places",
        description="The places Arsen named (Home, Office...), with how far each is from where he is now.",
        read_only=True,
        idempotent=True,
    )
    async def _places(self) -> ToolResult:
        places = await self.service.store.places()
        if not places:
            return ToolResult.empty("No named places yet. location.place_save names where he is now.")
        held = await self.service.store.last()
        lines = []
        for p in places:
            line = f"- [{p.id}] {p.name} (radius {round(p.radius_m)} m)"
            if held is not None:
                line += f" - {haversine_km(held[0].lat, held[0].lon, p.lat, p.lon):,.1f} km from him now"
            lines.append(line)
        return ToolResult.data("\n".join(lines), count=len(places))

    @tool(
        "location.place_save",
        description="Name a place (by default where he is right now) so turns say 'at Home' / 'at the Office'. Only when he asks.",
        args=_SaveArgs,
    )
    async def _save(
        self, name: str, radius_m: float = 150, lat: float | None = None, lon: float | None = None
    ) -> ToolResult:
        if await self._by_run():
            return ToolResult.failure("only Arsen names places; ask him in his chat")
        if lat is None or lon is None:
            held = await self.service.store.last()
            if held is None:
                return ToolResult.failure("no location from his phone yet - give lat and lon, or ask him to share it")
            lat, lon = held[0].lat, held[0].lon
        try:
            place = await self.service.store.add_place(name, lat, lon, radius_m)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.data(f"Saved {place.name!r} ({place.id}), radius {round(place.radius_m)} m.")

    @tool("location.place_delete", description="Delete a named place.", args=_PlaceArgs, destructive=True)
    async def _delete(self, place: str) -> ToolResult:
        if await self._by_run():
            return ToolResult.failure("only Arsen deletes places; ask him in his chat")
        found = await self._find(place)
        if found is None:
            return ToolResult.failure(f"no place {place!r}")
        await self.service.store.delete_place(found.id)
        return ToolResult.data(f"Deleted {found.name!r}.")


def owntracks_fix(body: dict[str, Any]) -> DeviceLocation | None:
    """OwnTracks HTTP mode posts {"_type": "location", lat, lon, acc, tst, vel(km/h)}; anything
    else it sends (waypoints, transitions, cards) is not a position."""
    if body.get("_type") != "location":
        return None
    with contextlib.suppress(KeyError, TypeError, ValueError):
        vel = body.get("vel")
        return DeviceLocation(
            lat=float(body["lat"]),
            lon=float(body["lon"]),
            accuracy_m=float(body["acc"]) if body.get("acc") is not None else None,
            taken_at=datetime.fromtimestamp(int(body["tst"]), UTC),
            speed_mps=float(vel) / 3.6 if vel is not None and float(vel) >= 0 else None,
            source="owntracks",
        )
    return None
