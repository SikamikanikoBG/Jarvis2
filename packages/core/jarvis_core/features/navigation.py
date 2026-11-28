"""Jarvis as a guide on the way (2026-11-28: "take me to A; he knows where I am and how fast, so he
says turn now, at the end you will see a statue, go past it and turn right").

``nav.start`` finds the place (his named places first, then OpenStreetMap's Nominatim), asks the
FOSSGIS OSRM servers for a walking, cycling or driving route with its manoeuvres, and words each
manoeuvre for speaking - with a landmark at the corner when OpenStreetMap has one (Overpass,
best effort: the public servers are often full, and guidance goes on without them).

Every fix from his phone (features/location.py) moves the guidance on, without a model in the
loop - a turn cannot wait for one:

- "In 120 m, turn left onto Graf Ignatiev." when the next manoeuvre comes within reach,
- "Turn left onto Graf Ignatiev, at the pharmacy Sopharma." when it is close,
- the next one once he has passed it,
- off the route on two fixes in a row: a new route from where he is,
- "You have arrived." within reach of the end, and the guidance ends.

Each line goes out as a ``nav.prompt`` event: the phone speaks it on a call and shows it
otherwise. The turn's context (NavBlock) tells the model where the guidance stands, so it can talk
about the way without repeating what the phone already said.

Only start and end of the route and the corners go to the outside services, without his name.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.engine.current import current_conversation_id
from jarvis_core.features.location import LocationService, haversine_km
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import DeviceLocation, NavStep, NavView, Run, RunKind, ToolResult
from jarvis_proto.events import NavPrompt, NavUpdated

log = logging.getLogger(__name__)

Mode = Literal["walking", "cycling", "driving"]

# A generic client name: these are public services, and nothing about him goes in a header.
USER_AGENT = "Jarvis2-selfhosted-assistant/1.0"
ROUTERS: dict[str, str] = {
    "walking": "https://routing.openstreetmap.de/routed-foot/route/v1/driving/",
    "cycling": "https://routing.openstreetmap.de/routed-bike/route/v1/driving/",
    "driving": "https://routing.openstreetmap.de/routed-car/route/v1/driving/",
}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
# The OSM keys a corner is named by, in the order a person would point at them.
LANDMARK_KEYS = ("historic", "tourism", "amenity", "leisure", "shop")
LANDMARK_RADIUS_M = 35


@dataclass(frozen=True)
class Reach:
    """Distances in metres, per mode."""

    far: float  # say what comes next
    near: float  # say it again, now
    passed: float  # this close = done with it
    arrive: float
    off: float  # this far from the line = off the route


REACH: dict[str, Reach] = {
    "walking": Reach(far=120, near=25, passed=12, arrive=20, off=35),
    "cycling": Reach(far=200, near=45, passed=20, arrive=25, off=50),
    "driving": Reach(far=500, near=150, passed=35, arrive=40, off=70),
}
REROUTE_GAP_S = 20


def _now() -> datetime:
    return datetime.now(UTC)


# --- wording ------------------------------------------------------------------------------------


def compass(bearing: float) -> str:
    names = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")
    return names[round((bearing % 360) / 45) % 8]


def ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth"}.get(n, f"{n}th")


def _turn(modifier: str) -> str:
    if modifier == "uturn":
        return "make a U-turn"
    if modifier == "straight":
        return "go straight on"
    if modifier.startswith("slight "):
        return f"bear {modifier.split()[1]}"
    if modifier.startswith("sharp "):
        return f"turn sharp {modifier.split()[1]}"
    return f"turn {modifier}"


def instruction(step: dict[str, Any], destination: str) -> str:
    """An OSRM step's manoeuvre in a sentence to say."""
    m = step.get("maneuver") or {}
    kind = str(m.get("type") or "turn")
    mod = str(m.get("modifier") or "straight")
    side = mod.rsplit(maxsplit=1)[-1] if mod not in ("straight", "uturn") else ""
    name = str(step.get("name") or "").strip()
    onto = f" onto {name}" if name else ""
    if kind == "depart":
        return f"head {compass(float(m.get('bearing_after') or 0))}" + (f" on {name}" if name else "")
    if kind == "arrive":
        where = f", on your {side}" if side in ("left", "right") else ""
        return f"{destination} is ahead{where}"
    if kind in ("roundabout", "rotary"):
        exit_ = m.get("exit") or step.get("exit")
        return (
            f"at the roundabout take the {ordinal(int(exit_))} exit{onto}"
            if exit_
            else f"go round the roundabout{onto}"
        )
    if kind in ("continue", "new name") and mod == "straight":
        return f"continue straight on{onto}"
    if kind == "merge":
        return f"merge {side or 'ahead'}{onto}"
    if kind == "on ramp":
        return f"take the ramp on the {side or 'right'}{onto}"
    if kind == "off ramp":
        return f"take the exit on the {side or 'right'}{onto}"
    if kind == "fork":
        return f"keep {side or 'straight'}{onto}"
    if kind == "end of road":
        return f"at the end of the road {_turn(mod)}{onto}"
    return f"{_turn(mod)}{onto}"


def spoken_distance(m: float) -> str:
    if m < 100:
        return f"{max(10, round(m / 10) * 10)} m"
    if m < 1000:
        return f"{round(m / 50) * 50} m"
    return f"{m / 1000:.1f} km"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- geometry -----------------------------------------------------------------------------------


def to_line_m(lat: float, lon: float, line: list[list[float]]) -> float:
    """Metres from a point to a polyline of [lon, lat] (local flat projection: fine for a walk)."""
    if not line:
        return math.inf
    k = math.cos(math.radians(lat)) * 111_320
    px, py = lon * k, lat * 110_574
    best = math.inf
    pts = [(p[0] * k, p[1] * 110_574) for p in line]
    for (ax, ay), (bx, by) in zip(pts, pts[1:] or pts, strict=False):
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def _m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    return haversine_km(a_lat, a_lon, b_lat, b_lon) * 1000


# --- the outside services -----------------------------------------------------------------------


class Providers:
    """Routing, geocoding and landmarks. A class so tests replace it whole."""

    def __init__(self, timeout_s: float = 15) -> None:
        self.timeout_s = timeout_s

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s, headers={"User-Agent": USER_AGENT})

    async def route(self, mode: str, a: tuple[float, float], b: tuple[float, float]) -> dict[str, Any]:
        url = f"{ROUTERS[mode]}{a[1]},{a[0]};{b[1]},{b[0]}"
        async with self._client() as c:
            r = await c.get(url, params={"overview": "full", "geometries": "geojson", "steps": "true"})
            r.raise_for_status()
            data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(f"no {mode} route: {data.get('message') or data.get('code')}")
        return data["routes"][0]

    async def geocode(self, query: str, near: tuple[float, float] | None) -> tuple[str, float, float] | None:
        params: dict[str, Any] = {"format": "jsonv2", "limit": 1, "q": query}
        if near is not None:
            # Prefer what is near him without excluding the rest of the world.
            params["viewbox"] = f"{near[1] - 0.3},{near[0] + 0.3},{near[1] + 0.3},{near[0] - 0.3}"
        async with self._client() as c:
            r = await c.get(NOMINATIM, params=params)
            r.raise_for_status()
            hits = r.json()
        if not hits:
            return None
        h = hits[0]
        return str(h.get("display_name") or query), float(h["lat"]), float(h["lon"])

    async def landmarks(self, points: list[tuple[float, float]]) -> list[str | None]:
        """The most notable named thing at each corner, or None. Best effort."""
        if not points:
            return []
        parts = "".join(
            f"nwr(around:{LANDMARK_RADIUS_M},{lat},{lon})[name][{key}];" for lat, lon in points for key in LANDMARK_KEYS
        )
        query = f"[out:json][timeout:10];({parts});out tags center;"
        data = None
        for url in OVERPASS:
            with contextlib.suppress(Exception):
                async with httpx.AsyncClient(timeout=12, headers={"User-Agent": USER_AGENT}) as c:
                    r = await c.post(url, data={"data": query})
                    if r.status_code == 200:
                        data = r.json()
                        break
        if data is None:
            return [None] * len(points)
        things = []
        for e in data.get("elements") or []:
            tags = e.get("tags") or {}
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            key = next((k for k in LANDMARK_KEYS if k in tags), None)
            if lat is None or lon is None or key is None:
                continue
            what = str(tags[key]).replace("_", " ")
            things.append((float(lat), float(lon), LANDMARK_KEYS.index(key), f"the {what} {tags['name']}"))
        out: list[str | None] = []
        for lat, lon in points:
            near = [
                (rank, _m(lat, lon, t_lat, t_lon), text)
                for t_lat, t_lon, rank, text in things
                if _m(lat, lon, t_lat, t_lon) <= LANDMARK_RADIUS_M
            ]
            out.append(min(near)[2] if near else None)
        return out


# --- the session --------------------------------------------------------------------------------


@dataclass
class Session:
    destination: str
    dest_lat: float
    dest_lon: float
    mode: str
    steps: list[dict[str, Any]]
    line: list[list[float]]
    total_m: float
    total_s: float
    conversation_id: str | None = None
    next_index: int = 1
    far_said: bool = False
    near_said: bool = False
    min_d: float = math.inf
    off_count: int = 0
    last_reroute: str | None = None
    started_at: str = field(default_factory=lambda: _now().isoformat())

    def step(self, i: int) -> NavStep:
        return NavStep(**self.steps[i])


def build_steps(route: dict[str, Any], destination: str) -> list[dict[str, Any]]:
    out = []
    for s in route["legs"][0]["steps"]:
        lon, lat = s["maneuver"]["location"]
        out.append(
            NavStep(
                instruction=instruction(s, destination),
                lat=lat,
                lon=lon,
                distance_m=float(s.get("distance") or 0),
                kind=str(s["maneuver"].get("type") or "turn"),
            ).model_dump()
        )
    return out


class NavService:
    def __init__(
        self,
        db: Database,
        bus: EventBus | None,
        location: LocationService,
        providers: Providers | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.db = db
        self.bus = bus
        self.location = location
        self.providers = providers or Providers()
        self._clock = clock
        self.session: Session | None = None

    # -- persistence --------------------------------------------------------------------------

    async def load(self) -> None:
        row = await self.db.fetchone("SELECT data FROM nav_session WHERE id = 1")
        if row is not None:
            with contextlib.suppress(Exception):
                data = json.loads(row["data"])
                data["min_d"] = math.inf if data.get("min_d") is None else data["min_d"]
                self.session = Session(**data)

    async def _save(self) -> None:
        if self.session is None:
            await self.db.execute("DELETE FROM nav_session")
            return
        data = asdict(self.session)
        data["min_d"] = None if math.isinf(data["min_d"]) else data["min_d"]
        await self.db.execute(
            "INSERT INTO nav_session(id, data, updated_at) VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (json.dumps(data), self._clock().isoformat()),
        )

    # -- what the phone and the model see ----------------------------------------------------

    def view(self, fix: DeviceLocation | None = None) -> NavView | None:
        s = self.session
        if s is None:
            return None
        i = min(s.next_index, len(s.steps) - 1)
        nxt = s.step(i)
        to_next = _m(fix.lat, fix.lon, nxt.lat, nxt.lon) if fix else None
        rest = sum(float(st["distance_m"]) for st in s.steps[i:])
        left = (to_next or 0) + rest if fix else None
        eta = s.total_s * left / s.total_m if left is not None and s.total_m else None
        return NavView(
            destination=s.destination,
            mode=s.mode,  # type: ignore[arg-type]
            next_instruction=_capital(nxt.instruction),
            next_landmark=nxt.landmark,
            to_next_m=round(to_next) if to_next is not None else None,
            left_m=round(left) if left is not None else None,
            eta_s=round(eta) if eta is not None else None,
            step=i,
            steps=len(s.steps) - 1,
            conversation_id=s.conversation_id,
            started_at=datetime.fromisoformat(s.started_at),
        )

    def _publish(self, fix: DeviceLocation | None, prompts: list[tuple[str, str]]) -> None:
        if self.bus is None:
            return
        for kind, text in prompts:
            self.bus.publish(NavPrompt(text=text, kind=kind))
        self.bus.publish(NavUpdated(view=self.view(fix)))

    # -- start and stop ---------------------------------------------------------------------

    async def _resolve(self, to: str, here: DeviceLocation) -> tuple[str, float, float]:
        to = to.strip()
        with contextlib.suppress(ValueError):
            lat_s, lon_s = (x.strip() for x in to.split(",", 1))
            return f"{float(lat_s):.5f}, {float(lon_s):.5f}", float(lat_s), float(lon_s)
        for p in await self.location.store.places():
            if p.name.casefold() == to.casefold():
                return p.name, p.lat, p.lon
        hit = await self.providers.geocode(to, (here.lat, here.lon))
        if hit is None:
            raise ValueError(f"could not find {to!r} on the map - give the address, or lat,lon")
        name, lat, lon = hit
        return name.split(",")[0].strip() or to, lat, lon

    async def _route(self, here: DeviceLocation, s_mode: str, dest: tuple[str, float, float]) -> Session:
        name, lat, lon = dest
        route = await self.providers.route(s_mode, (here.lat, here.lon), (lat, lon))
        steps = build_steps(route, name)
        corners = [(st["lat"], st["lon"]) for st in steps[1:-1]]
        try:
            marks = await self.providers.landmarks(corners)
        except Exception:
            marks = [None] * len(corners)
        for st, mark in zip(steps[1:-1], marks, strict=False):
            st["landmark"] = mark
        return Session(
            destination=name,
            dest_lat=lat,
            dest_lon=lon,
            mode=s_mode,
            steps=steps,
            line=[list(p) for p in route["geometry"]["coordinates"]],
            total_m=float(route["distance"]),
            total_s=float(route["duration"]),
        )

    async def start(self, to: str, mode: str = "walking", conversation_id: str | None = None) -> str:
        if mode not in REACH:
            raise ValueError(f"mode must be one of {', '.join(REACH)}")
        held = await self.location.store.last()
        if held is None:
            raise ValueError("no location from his phone - he has to switch on sharing (Settings > Location)")
        here, _ = held
        if (self._clock() - here.taken_at).total_seconds() > 15 * 60:
            raise ValueError(
                "his last location is over 15 minutes old - Jarvis must be open on his phone, sharing location"
            )
        dest = await self._resolve(to, here)
        session = await self._route(here, mode, dest)
        session.conversation_id = conversation_id
        self.session = session
        await self._save()
        first = session.step(0).instruction
        second = session.step(1) if len(session.steps) > 2 else None
        line = (
            f"On our way to {session.destination}: {spoken_distance(session.total_m)}, about "
            f"{max(1, round(session.total_s / 60))} min {mode}. {_capital(first)}."
        )
        if second is not None:
            line += f" Then in {spoken_distance(session.steps[0]['distance_m'])}, {second.instruction}."
        self._publish(here, [("start", line)])
        return line

    async def stop(self, why: str = "stopped") -> bool:
        if self.session is None:
            return False
        self.session = None
        await self._save()
        if self.bus is not None:
            self.bus.publish(NavUpdated(view=None))
        log.info("navigation %s", why)
        return True

    # -- every fix ---------------------------------------------------------------------------

    async def on_fix(self, fix: DeviceLocation) -> list[tuple[str, str]]:
        s = self.session
        if s is None:
            return []
        r = REACH[s.mode]
        slack = min(fix.accuracy_m or 0, 30)
        prompts: list[tuple[str, str]] = []

        if _m(fix.lat, fix.lon, s.dest_lat, s.dest_lon) <= r.arrive + slack:
            prompts.append(("arrive", f"You have arrived at {s.destination}."))
            self._publish(fix, prompts)
            await self.stop("arrived")
            return prompts

        if to_line_m(fix.lat, fix.lon, s.line) > r.off + min(fix.accuracy_m or 0, 50):
            s.off_count += 1
        else:
            s.off_count = 0
        last = datetime.fromisoformat(s.last_reroute) if s.last_reroute else None
        if s.off_count >= 2 and (last is None or (self._clock() - last).total_seconds() >= REROUTE_GAP_S):
            try:
                fresh = await self._route(fix, s.mode, (s.destination, s.dest_lat, s.dest_lon))
            except Exception as exc:
                log.warning("reroute failed: %s", exc)
                s.last_reroute = self._clock().isoformat()
                await self._save()
                return []
            fresh.conversation_id, fresh.started_at = s.conversation_id, s.started_at
            fresh.last_reroute = self._clock().isoformat()
            self.session = s = fresh
            nxt = s.step(min(1, len(s.steps) - 1))
            prompts.append(
                (
                    "reroute",
                    f"Off the route - new way: {s.step(0).instruction}, then in "
                    f"{spoken_distance(s.steps[0]['distance_m'])}, {nxt.instruction}.",
                )
            )
            await self._save()
            self._publish(fix, prompts)
            return prompts

        while s.next_index < len(s.steps):
            step = s.step(s.next_index)
            d = _m(fix.lat, fix.lon, step.lat, step.lon)
            gone_past = s.near_said and d > s.min_d + r.passed
            if step.kind != "arrive" and (d <= r.passed or gone_past):
                s.next_index += 1
                s.far_said = s.near_said = False
                s.min_d = math.inf
                continue
            s.min_d = min(s.min_d, d)
            at = f", at {step.landmark}" if step.landmark else ""
            if step.kind == "arrive":
                if not s.far_said and d <= r.far:
                    prompts.append(("far", f"In {spoken_distance(d)}, {step.instruction}."))
                    s.far_said = True
            elif not s.near_said and d <= r.near:
                prompts.append(("near", f"{_capital(step.instruction)}{at}."))
                s.near_said = s.far_said = True
            elif not s.far_said and d <= r.far:
                prompts.append(("far", f"In {spoken_distance(d)}, {step.instruction}{at}."))
                s.far_said = True
            break
        await self._save()
        self._publish(fix, prompts)
        return prompts


class NavBlock:
    """While guiding: where the guidance stands, so the model can talk about the way."""

    def __init__(self, service: NavService) -> None:
        self._service = service

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if self._service.session is None or run.kind not in (RunKind.CHAT, RunKind.TASK):
            return None
        held = await self._service.location.store.last()
        v = self._service.view(held[0] if held else None)
        if v is None:
            return None
        bits = [f"## Guiding Arsen\nTo {v.destination}, {v.mode}."]
        if v.left_m is not None:
            eta = f", about {max(1, round((v.eta_s or 0) / 60))} min" if v.eta_s else ""
            bits.append(f"{spoken_distance(v.left_m)} left{eta}.")
        if v.next_instruction:
            dist = f"in {spoken_distance(v.to_next_m)}, " if v.to_next_m is not None else ""
            mark = f" (at {v.next_landmark})" if v.next_landmark else ""
            bits.append(f"Next: {dist}{v.next_instruction.lower()}{mark}.")
        bits.append(
            "His phone says every turn itself as he reaches it; do not repeat them unless he asks."
            " nav.stop ends the guidance, nav.start with a new place changes it."
        )
        return " ".join(bits)


# --- tools --------------------------------------------------------------------------------------


class _StartArgs(BaseModel):
    to: str = Field(description="Where to: one of his named places, an address or a place name, or 'lat,lon'")
    mode: Mode = Field(default="walking", description="walking (also running), cycling or driving")


class NavTools(BuiltinProvider):
    """``nav.*``: guide him somewhere, turn by turn, while he talks to Jarvis on the way."""

    name = "nav"

    def __init__(self, service: NavService) -> None:
        self.service = service
        super().__init__()

    @tool(
        "nav.start",
        description=(
            "Guide Arsen to a place, turn by turn, from where his phone is now. His phone then speaks each "
            "turn itself as he reaches it (with a landmark when one is known), re-routes when he leaves the "
            "route, and says when he has arrived. Returns the overview to tell him."
        ),
        args=_StartArgs,
    )
    async def _start(self, to: str, mode: Mode = "walking") -> ToolResult:
        try:
            line = await self.service.start(to, mode, conversation_id=current_conversation_id.get())
        except (ValueError, httpx.HTTPError) as exc:
            return ToolResult.failure(f"could not start guidance: {exc}")
        return ToolResult.data(line + " (His phone already said this; just confirm briefly.)")

    @tool(
        "nav.status",
        description="Where the guidance stands: next turn, distance left, time left.",
        read_only=True,
        idempotent=True,
    )
    async def _status(self) -> ToolResult:
        held = await self.service.location.store.last()
        v = self.service.view(held[0] if held else None)
        if v is None:
            return ToolResult.empty("Not guiding anywhere.")
        return ToolResult.data(json.dumps(v.model_dump(mode="json")))

    @tool("nav.stop", description="End the guidance.")
    async def _stop(self) -> ToolResult:
        return (
            ToolResult.data("Guidance ended.")
            if await self.service.stop()
            else ToolResult.empty("Not guiding anywhere.")
        )


__all__ = ["NavBlock", "NavService", "NavTools", "Providers", "instruction", "to_line_m"]
