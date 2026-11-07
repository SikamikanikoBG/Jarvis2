"""The Context screen's data (features/context_usage.py): what the model calls hold of their
context windows right now, and how that went over an hour, a day, a week, a month."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request

from jarvis_core.api.deps import core_of, require_token
from jarvis_core.features.context_usage import RANGES

router = APIRouter(prefix="/api/context", dependencies=[Depends(require_token)])


@router.get("/live")
async def context_live(request: Request) -> dict[str, Any]:
    """Per endpoint: the calls in flight or waiting, by source; the last 15 minutes; overflows of
    the last 24 hours."""
    return await core_of(request).context_usage.live()


@router.get("/usage")
async def context_usage(request: Request, range: str = "24h", tz: str | None = None) -> dict[str, Any]:
    """Buckets over ``range`` (1h, 6h, 24h, 7d, 30d, 90d) in the clock of ``tz`` (an IANA name;
    the settings' timezone when omitted)."""
    core = core_of(request)
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    name = tz or core.settings.timezone
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(400, f"unknown timezone {name!r}") from exc
    return await core.context_usage.usage(range, name)
