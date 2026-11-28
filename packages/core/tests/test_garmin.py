"""His Garmin watch through the Personal API (features/garmin.py) - against a fake Garmin."""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta

import httpx
import pytest

from jarvis_core.features.garmin import GarminApi, GarminError, GarminService, digest, find
from jarvis_proto import ConversationKind, Run, RunKind, new_id
from jarvis_proto.settings import GarminSettings
from tests.conftest import Harness


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis_core.features.garmin as G

    monkeypatch.setattr(G, "HISTORY_PAUSE_S", 0)


def jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class FakeGarmin:
    def __init__(self, today: str) -> None:
        self.today = today
        self.calls: list[tuple[str, str]] = []
        self.tokens = 0
        self.expire_first = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        auth = req.headers.get("Authorization", "")
        self.calls.append((path, auth))
        if path.endswith("/personal-api-key/token"):
            assert auth == "ApiKey KEY-123"
            self.tokens += 1
            return httpx.Response(200, text=jwt(time.time() + 86400))
        assert auth.startswith("Bearer ")
        if self.expire_first:
            self.expire_first = False
            return httpx.Response(401)
        day = req.url.params.get("date") or req.url.params.get("startDate") or path.rstrip("/").split("/")[-1]
        today = day == self.today
        if "dailySleep" in path:
            secs = 22_200 if today else 25_200
            return httpx.Response(
                200,
                json={
                    "dailySleepDTO": {
                        "sleepTimeSeconds": secs,
                        "deepSleepSeconds": 3600,
                        "sleepScores": {"overall": {"value": 64}},
                    }
                },
            )
        if "dailysummaries" in path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "calendarDate": day,
                            "totalSteps": 4200,
                            "restingHeartRate": 57 if today else 54,
                            "bodyBatteryMostRecentValue": 35,
                            "averageStressLevel": 38,
                        }
                    ]
                },
            )
        if "bodyBattery" in path:
            return httpx.Response(
                200,
                json=[
                    {"charged": 48, "drained": 60, "bodyBatteryValuesArray": [[1, "MEASURED", 30], [2, "MEASURED", 35]]}
                ],
            )
        if "stress" in path:
            return httpx.Response(200, json=[{"values": {"overallStressLevel": 38}}])
        if "hrv-service" in path:
            return httpx.Response(
                200, json={"hrvSummary": {"lastNightAvg": 41, "weeklyAvg": 47, "status": "UNBALANCED"}}
            )
        if "dailyHeartRate" in path:
            return httpx.Response(200, json={"restingHeartRate": 57 if today else 54})
        if "activities" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "activityName": "Sofia Running",
                        "activityType": {"typeKey": "running"},
                        "startTimeLocal": "2026-11-27 07:10:00",
                        "distance": 8200,
                        "duration": 2700,
                        "averageHR": 148,
                    }
                ],
            )
        return httpx.Response(404)


def service(harness: Harness, fake: FakeGarmin, key: str = "KEY-123") -> GarminService:
    settings = GarminSettings(api_key=key)
    api = GarminApi(
        lambda: settings.api_key,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        base_url="https://g.test",
    )
    return GarminService(harness.core.db, lambda: settings, lambda: "Europe/Sofia", api=api)


def test_reading_garmin_answers_defensively():
    assert find({"a": [{"b": {"restingHeartRate": 55}}]}, "restingHeartRate") == 55
    assert find({"x": True}, "x") is None  # a bool is not a number
    d = digest({"sleep": {"dailySleepDTO": {"sleepTimeSeconds": 100}}, "stress": None})
    assert d == {"sleep_s": 100}  # what is not there is left out, not guessed


async def test_sync_stores_the_days_and_the_turn_hears_how_he_is(harness: Harness):
    g0 = service(harness, FakeGarmin("x"))
    today = g0.today().isoformat()
    fake = FakeGarmin(today)
    g = service(harness, fake)
    out = await g.sync()
    assert out["failed"] == [] and fake.tokens == 1
    days = await g.days()
    assert len(days) == 31 and days[-1] == today  # first sync: 30 days back for the baselines
    text = await g.describe()
    assert text is not None
    for part in (
        "Slept 6 h 10 (score 64); his 30-day average is 7 h 00.",
        "Body Battery 35 now (charged +48 overnight).",
        "Resting heart rate 57 (+3 against his 30-day 54).",
        "HRV last night 41 ms, weekly 47, unbalanced.",
        "Average stress today 38.",
        "4,200 steps so far.",
        "Last activity: running 8.2 km 45 min",
    ):
        assert part in text, part
    # a second sync is today and yesterday only, on the same token
    n = len(fake.calls)
    await g.sync()
    assert fake.tokens == 1 and len(fake.calls) - n == 6 * 2 + 1


async def test_a_token_that_died_early_is_renewed_once(harness: Harness):
    fake = FakeGarmin("x")
    g = service(harness, fake)
    fake.expire_first = True
    assert await g.api.get("/hrv-service/hrv/2026-11-28") is not None
    assert fake.tokens == 2


async def test_no_key_no_sync_and_nobody_else_hears_it(harness: Harness):
    with pytest.raises(GarminError):
        await service(harness, FakeGarmin("x"), key="").sync()
    core = harness.core
    core.settings.garmin.api_key = "KEY-123"
    fake = FakeGarmin(core.garmin.today().isoformat())
    core.garmin.api = GarminApi(
        lambda: "KEY-123", client=httpx.AsyncClient(transport=httpx.MockTransport(fake)), base_url="https://g.test"
    )
    await core.garmin.sync()

    async def blocks(kind: RunKind, incognito: bool = False) -> dict[str, str]:
        conv = await core.store.create_conversation(kind=ConversationKind.CHAT, incognito=incognito)
        return dict(
            await core.context.turn_blocks(Run(id=new_id("run"), conversation_id=conv.id, kind=kind), skill_names=[])
        )

    assert "Body Battery 35" in (await blocks(RunKind.CHAT))["BodyBlock"]
    assert "BodyBlock" in await blocks(RunKind.SCHEDULED)
    assert "BodyBlock" not in await blocks(RunKind.COLLAB)
    assert "BodyBlock" not in await blocks(RunKind.MAIL)
    assert "BodyBlock" not in await blocks(RunKind.CHAT, incognito=True)
    _ = timedelta


def test_the_key_never_reaches_jarvis_in_clear(tmp_path):  # type: ignore[no-untyped-def]
    """The owner's own settings route shows secrets in clear (rest.get_settings); Jarvis's
    settings tool and the change trail mask them, like every other secret."""
    import asyncio as aio

    from fastapi.testclient import TestClient

    from jarvis_core.app import Core, create_app
    from jarvis_core.config import CoreConfig

    core = Core(CoreConfig(home=tmp_path, token="t"))
    with TestClient(create_app(core.config, core=core)) as c:
        h = {"Authorization": "Bearer t"}
        doc = c.get("/api/settings", headers=h).json()
        doc["garmin"]["api_key"] = "REAL-KEY-42"
        assert c.patch("/api/settings", json={"garmin": doc["garmin"]}, headers=h).status_code == 200
        assert core.settings.garmin.api_key == "REAL-KEY-42"
        assert c.get("/api/garmin", headers=h).json()["configured"] is True
        seen = c.portal.call(
            lambda: core.registry.call(
                "settings.get", {"path": "garmin.api_key"}, cancel=aio.Event(), idempotency_key="g"
            )
        )  # type: ignore[union-attr]
        assert "REAL-KEY-42" not in seen.text
        trail = c.get("/api/settings/changes?path=garmin.api_key", headers=h).json()
        assert trail and "REAL-KEY-42" not in str(trail)
