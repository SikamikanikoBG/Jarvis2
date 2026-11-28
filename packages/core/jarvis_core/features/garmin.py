"""His Garmin watch (2026-11-28: "so he knows my activities, pulse, sleep, energy - nudges me to
live healthy and fits my schedule to my condition").

Through the official Garmin Connect **Personal API** (October 2026): the key from
connect.garmin.com/app/settings/personalApiToken is exchanged for a 24-hour bearer token
(``POST /personalapitoken-service/personal-api/personal-api-key/token`` with
``Authorization: ApiKey <key>``; the answer is the JWT itself), and the token reads the same
paths Garmin's own client uses (github.com/garmin/connect-personal-api-python, client.py).

Every ``sync_every_min`` today and yesterday are pulled - daily summary, sleep, Body Battery,
stress, HRV, heart rate - and the last week's activities; once, the 30 days before, for the
baselines. Each answer is kept as it came, per day and kind, for ``keep_days``.

Garmin does not document the field names. They are read defensively (``find``: the first
number under any of the names Garmin Connect is known to use, at any depth), a field that is not
found is left out rather than guessed, and ``garmin.raw`` shows Jarvis the answer itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Run, RunKind, ToolResult
from jarvis_proto.settings import GarminSettings

if TYPE_CHECKING:
    from jarvis_core.db.store import Store

log = logging.getLogger(__name__)

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
REFRESH_BEFORE_S = 3600
BACKFILL_DAYS = 30
# Between history reads on the first sync: gentle on Garmin (it publishes no rate limit).
HISTORY_PAUSE_S = 0.5
RAW_SHOWN = 6_000


def day_paths(d: str) -> dict[str, tuple[str, dict[str, str]]]:
    """The per-day reads, as Garmin's own client makes them."""
    return {
        "summary": ("/usersummary-service/v2/dailysummaries", {"startDate": d, "endDate": d}),
        "sleep": ("/sleep-service/sleep/dailySleep", {"date": d}),
        "body_battery": ("/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": d, "endDate": d}),
        "stress": (f"/usersummary-service/stats/stress/daily/{d}/{d}", {}),
        "hrv": (f"/hrv-service/hrv/{d}", {}),
        "heart": ("/wellness-service/wellness/dailyHeartRate", {"date": d}),
    }


ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


class GarminError(RuntimeError):
    pass


def jwt_exp(token: str) -> float:
    try:
        part = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise GarminError("the token endpoint did not return a JWT with an exp claim") from exc


class GarminApi:
    def __init__(
        self, key: Callable[[], str], client: httpx.AsyncClient | None = None, base_url: str = BASE_URL
    ) -> None:
        self._key = key
        self._client = client or httpx.AsyncClient(timeout=30)
        self.base_url = base_url.rstrip("/")
        self._token: tuple[str, float] | None = None
        self._token_for: str | None = None

    async def token(self, *, force: bool = False) -> str:
        key = self._key().strip()
        if not key:
            raise GarminError("no Garmin key: Settings > Garmin")
        if not force and self._token and self._token_for == key and self._token[1] - time.time() > REFRESH_BEFORE_S:
            return self._token[0]
        r = await self._client.post(
            f"{self.base_url}{TOKEN_PATH}", headers={"Accept": "*/*", "Authorization": f"ApiKey {key}"}
        )
        if r.status_code in (401, 403):
            raise GarminError("Garmin refused the key - expired or deleted? Make a new one at connect.garmin.com")
        r.raise_for_status()
        token = r.text.strip().strip('"')
        self._token, self._token_for = (token, jwt_exp(token)), key
        return token

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in (0, 1):
            token = await self.token(force=attempt == 1)
            r = await self._client.get(
                f"{self.base_url}{path}",
                params=params or None,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
            if r.status_code == 401 and attempt == 0:
                continue  # the token died early: one fresh one
            if r.status_code == 204 or not r.content:
                return None
            r.raise_for_status()
            return r.json()
        raise GarminError("unauthorised twice")


# --- reading Garmin's answers ---------------------------------------------------------------------


def walk(obj: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def find(obj: Any, *names: str) -> float | None:
    """The first number under any of `names` (in that order of preference), at any depth."""
    for name in names:
        for k, v in walk(obj):
            if k == name and isinstance(v, int | float) and not isinstance(v, bool):
                return float(v)
    return None


def find_text(obj: Any, *names: str) -> str | None:
    for name in names:
        for k, v in walk(obj):
            if k == name and isinstance(v, str) and v:
                return v
    return None


def sleep_score(sleep: Any) -> float | None:
    for k, v in walk(sleep):
        if k == "sleepScores" and isinstance(v, dict):
            overall = v.get("overall")
            if isinstance(overall, dict) and isinstance(overall.get("value"), int | float):
                return float(overall["value"])
    return find(sleep, "overallSleepScore", "sleepScore")


def body_battery_now(bb: Any, summary: Any) -> float | None:
    v = find(summary, "bodyBatteryMostRecentValue")
    if v is not None:
        return v
    for k, arr in walk(bb):
        if k == "bodyBatteryValuesArray" and isinstance(arr, list) and arr:
            last = arr[-1]
            if isinstance(last, list):
                nums = [x for x in last[1:] if isinstance(x, int | float) and not isinstance(x, bool)]
                if nums:
                    return float(nums[-1])
    return None


def digest(raw: dict[str, Any]) -> dict[str, Any]:
    """One day's numbers, each only when Garmin's answer had it."""
    s, sl, bb, st, hrv, hr = (raw.get(k) for k in ("summary", "sleep", "body_battery", "stress", "hrv", "heart"))
    out: dict[str, Any] = {
        "sleep_s": find(sl, "sleepTimeSeconds"),
        "sleep_score": sleep_score(sl),
        "deep_s": find(sl, "deepSleepSeconds"),
        "rem_s": find(sl, "remSleepSeconds"),
        "resting_hr": find(hr, "restingHeartRate") or find(s, "restingHeartRate"),
        "steps": find(s, "totalSteps"),
        "stress_avg": find(st, "overallStressLevel", "averageStressLevel", "avgStressLevel")
        or find(s, "averageStressLevel"),
        "body_battery": body_battery_now(bb, s),
        "bb_charged": find(bb, "charged") or find(s, "bodyBatteryChargedValue"),
        "bb_drained": find(bb, "drained") or find(s, "bodyBatteryDrainedValue"),
        "hrv_night": find(hrv, "lastNightAvg"),
        "hrv_week": find(hrv, "weeklyAvg"),
        "hrv_status": find_text(hrv, "status"),
        "active_kcal": find(s, "activeKilocalories"),
    }
    return {k: v for k, v in out.items() if v is not None}


def activities(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else (raw or {}).get("activities") or [] if isinstance(raw, dict) else []
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        kind = a.get("activityType")
        out.append(
            {
                "name": a.get("activityName"),
                "type": kind.get("typeKey") if isinstance(kind, dict) else kind,
                "start": a.get("startTimeLocal") or a.get("startTimeGMT"),
                "km": round(float(a["distance"]) / 1000, 2) if isinstance(a.get("distance"), int | float) else None,
                "min": round(float(a["duration"]) / 60) if isinstance(a.get("duration"), int | float) else None,
                "avg_hr": a.get("averageHR"),
            }
        )
    return out


def _hm(seconds: float) -> str:
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m:02d}"


# --- the service ----------------------------------------------------------------------------------


class GarminService:
    def __init__(
        self, db: Database, settings: Callable[[], GarminSettings], tz: Callable[[], str], api: GarminApi | None = None
    ) -> None:
        self.db = db
        self._settings = settings
        self._tz = tz
        self.api = api or GarminApi(lambda: self._settings().api_key)
        self.last_sync: datetime | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> GarminSettings:
        return self._settings()

    def configured(self) -> bool:
        s = self.settings
        return s.enabled and bool(s.api_key.strip()) and not s.api_key.startswith("••")

    def today(self) -> date:
        return datetime.now(ZoneInfo(self._tz())).date()

    async def _put(self, day: str, kind: str, data: Any) -> None:
        await self.db.execute(
            "INSERT INTO garmin_raw(day, kind, data, fetched_at) VALUES (?,?,?,?)"
            " ON CONFLICT(day, kind) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
            (day, kind, json.dumps(data), datetime.now(UTC).isoformat()),
        )

    async def raw(self, day: str) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT kind, data FROM garmin_raw WHERE day = ?", (day,))
        return {r["kind"]: json.loads(r["data"]) for r in rows}

    async def days(self) -> list[str]:
        rows = await self.db.fetchall("SELECT DISTINCT day FROM garmin_raw ORDER BY day")
        return [r["day"] for r in rows]

    async def sync(self, *, backfill: bool = False) -> dict[str, Any]:
        """Pull today and yesterday (and, the first time, the 30 days before); prune old days."""
        if not self.configured():
            raise GarminError("Garmin is off or has no key (Settings > Garmin)")
        async with self._lock:
            today = self.today()
            have = set(await self.days())
            wanted = [today, today - timedelta(days=1)]
            if backfill or not have:
                wanted += [today - timedelta(days=i) for i in range(2, BACKFILL_DAYS + 1)]
            got, failed = 0, []
            try:
                for d in wanted:
                    iso = d.isoformat()
                    paths = day_paths(iso)
                    kinds = list(paths) if d >= today - timedelta(days=1) else ["summary", "sleep", "hrv"]
                    for kind in kinds:
                        path, params = paths[kind]
                        try:
                            await self._put(iso, kind, await self.api.get(path, params))
                            got += 1
                        except httpx.HTTPStatusError as exc:
                            failed.append(f"{iso} {kind}: HTTP {exc.response.status_code}")
                        if d < today - timedelta(days=1):
                            await asyncio.sleep(HISTORY_PAUSE_S)
                acts = await self.api.get(
                    ACTIVITIES_PATH,
                    {
                        "start": 0,
                        "limit": 20,
                        "startDate": (today - timedelta(days=7)).isoformat(),
                        "endDate": today.isoformat(),
                    },
                )
                await self._put(today.isoformat(), "activities", acts)
            except (GarminError, httpx.HTTPError) as exc:
                self.last_error = str(exc)
                raise GarminError(str(exc)) from exc
            cutoff = (today - timedelta(days=self.settings.keep_days)).isoformat()
            await self.db.execute("DELETE FROM garmin_raw WHERE day < ?", (cutoff,))
            self.last_sync = datetime.now(UTC)
            self.last_error = "; ".join(failed[:5]) or None
            return {"stored": got, "failed": failed}

    # -- the loop --------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="garmin-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(20)  # let the core come up first
        while True:
            if self.configured():
                try:
                    await self.sync()
                except Exception as exc:
                    self.last_error = str(exc)
                    log.warning("garmin sync failed: %s", exc)
            await asyncio.sleep(self.settings.sync_every_min * 60)

    # -- what a turn is told --------------------------------------------------------------

    async def baseline(self, field: str, days: int = 30) -> float | None:
        today = self.today()
        vals = []
        for i in range(1, days + 1):
            d = (today - timedelta(days=i)).isoformat()
            v = digest(await self.raw(d)).get(field)
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if len(vals) >= 5 else None

    async def describe(self) -> str | None:
        today = self.today().isoformat()
        raw = await self.raw(today)
        if not raw:
            return None
        d = digest(raw)
        parts: list[str] = []
        if "sleep_s" in d:
            line = f"Slept {_hm(d['sleep_s'])}"
            if "sleep_score" in d:
                line += f" (score {round(d['sleep_score'])})"
            base = await self.baseline("sleep_s")
            if base:
                line += f"; his 30-day average is {_hm(base)}"
            parts.append(line + ".")
        if "body_battery" in d:
            line = f"Body Battery {round(d['body_battery'])} now"
            if "bb_charged" in d:
                line += f" (charged +{round(d['bb_charged'])} overnight)"
            parts.append(line + ".")
        if "resting_hr" in d:
            base = await self.baseline("resting_hr")
            diff = f" ({d['resting_hr'] - base:+.0f} against his 30-day {base:.0f})" if base else ""
            parts.append(f"Resting heart rate {round(d['resting_hr'])}{diff}.")
        if "hrv_night" in d:
            week = f", weekly {round(d['hrv_week'])}" if "hrv_week" in d else ""
            status = f", {d['hrv_status'].lower()}" if "hrv_status" in d else ""
            parts.append(f"HRV last night {round(d['hrv_night'])} ms{week}{status}.")
        if "stress_avg" in d:
            parts.append(f"Average stress today {round(d['stress_avg'])}.")
        if "steps" in d:
            parts.append(f"{round(d['steps']):,} steps so far.")
        acts = activities(raw.get("activities"))
        if acts:
            a = acts[0]
            bits = [
                str(x)
                for x in (
                    a["type"] or a["name"],
                    f"{a['km']} km" if a["km"] else None,
                    f"{a['min']} min" if a["min"] else None,
                )
                if x
            ]
            parts.append(f"Last activity: {' '.join(bits)} ({a['start']}).")
        if not parts:
            return None
        when = (
            f", synced {round((datetime.now(UTC) - self.last_sync).total_seconds() / 60)} min ago"
            if self.last_sync
            else ""
        )
        return f"## His body today (Garmin{when})\n" + " ".join(parts)


class BodyBlock:
    """Per-turn context for the runs that work for him: what his watch says today. Garmin data is
    his health: never in a run that answers someone else, never in a private chat."""

    def __init__(self, service: GarminService, store: Store) -> None:
        self._service = service
        self._store = store

    async def context_block(self, run: Run, *, skill_names: list[str]) -> str | None:
        if not self._service.settings.enabled or run.kind not in (RunKind.CHAT, RunKind.SCHEDULED, RunKind.TASK):
            return None
        conv = await self._store.get_conversation(run.conversation_id)
        if conv is not None and conv.incognito:
            return None
        text = await self._service.describe()
        if text is None:
            return None
        return text + (
            "\nUse it to look after him: weigh his plans against how rested he is, suggest a walk or an"
            " earlier night when the numbers say so - briefly, at a natural moment, and not again if he"
            " already heard it today. Do not lecture, and do not recite numbers he did not ask for."
        )


# --- tools ----------------------------------------------------------------------------------------


class _DayArgs(BaseModel):
    day: str | None = Field(default=None, description="YYYY-MM-DD; default today")


class _RawArgs(BaseModel):
    kind: str = Field(description="summary | sleep | body_battery | stress | hrv | heart | activities")
    day: str | None = Field(default=None, description="YYYY-MM-DD; default today")


class _TrendArgs(BaseModel):
    field: str = Field(description="sleep_s | sleep_score | resting_hr | steps | stress_avg | body_battery | hrv_night")
    days: int = Field(default=14, ge=2, le=90)


class GarminTools(BuiltinProvider):
    """``garmin.*``: his watch - today, any day, a trend, the raw answer."""

    name = "garmin"

    def __init__(self, service: GarminService) -> None:
        self.service = service
        super().__init__()

    def _day(self, day: str | None) -> str:
        return day or self.service.today().isoformat()

    @tool(
        "garmin.day",
        description="His Garmin numbers for a day: sleep, Body Battery, heart rate, HRV, stress, steps.",
        args=_DayArgs,
        read_only=True,
        idempotent=True,
    )
    async def _day_tool(self, day: str | None = None) -> ToolResult:
        d = self._day(day)
        raw = await self.service.raw(d)
        if not raw:
            return ToolResult.empty(
                f"No Garmin data for {d}."
                + ("" if self.service.configured() else " Garmin has no key yet (Settings > Garmin).")
            )
        return ToolResult.data(
            json.dumps({"day": d, **digest(raw), "activities": activities(raw.get("activities"))[:5]})
        )

    @tool(
        "garmin.trend",
        description="One of his Garmin numbers day by day, with its average.",
        args=_TrendArgs,
        read_only=True,
        idempotent=True,
    )
    async def _trend(self, field: str, days: int = 14) -> ToolResult:
        today = self.service.today()
        rows = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            v = digest(await self.service.raw(d)).get(field)
            if v is not None:
                rows.append(f"{d}: {round(v, 1)}")
        if not rows:
            return ToolResult.empty(f"No {field} in the last {days} days.")
        vals = [float(r.split(": ")[1]) for r in rows]
        return ToolResult.data("\n".join(rows) + f"\naverage {sum(vals) / len(vals):.1f}", count=len(rows))

    @tool(
        "garmin.raw",
        description="Garmin's own answer for a kind and day, as it came (trimmed) - for anything the summaries do not show.",
        args=_RawArgs,
        read_only=True,
        idempotent=True,
    )
    async def _raw(self, kind: str, day: str | None = None) -> ToolResult:
        data = (await self.service.raw(self._day(day))).get(kind)
        if data is None:
            return ToolResult.empty(f"No {kind} for {self._day(day)}.")
        text = json.dumps(data)
        return ToolResult.data(text[:RAW_SHOWN] + (" …(trimmed)" if len(text) > RAW_SHOWN else ""))

    @tool("garmin.sync", description="Pull the latest from Garmin now.")
    async def _sync(self) -> ToolResult:
        try:
            out = await self.service.sync()
        except GarminError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.data(
            f"Synced: {out['stored']} answers stored"
            + (f"; failed: {', '.join(out['failed'][:3])}" if out["failed"] else "")
        )
