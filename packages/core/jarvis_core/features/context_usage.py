"""Context usage: what the model calls hold of their context window, now and over time
(idea-to-uat/context-usage/01-plan.md).

The context ledger (models/ledger.py) knows the calls in flight; this writes each finished call
to ``model_calls`` and answers the Context screen:

- ``live()``: per endpoint (one GPU box behind one URL), the calls holding or waiting for it now,
  split by who they are for, the last 15 minutes' peaks, and the overflows of the last 24 hours.
- ``usage(range, tz)``: the same split over time, in buckets of the asker's own clock (days
  are local midnights, so a 23- or 25-hour DST day is one day), with peak context per call
  against the window, time the endpoint was busy, tokens processed, overflows, a per-source
  table and the hour-of-day profile of the last 7 days (which hours are already taken when a
  schedule is to be moved).

"Agents" are sources: chat, voice, each scheduled prompt by name, task, triage, mail, meeting,
collab, system, and housekeeping by purpose (journal, compaction, knowledge, or the role when
nothing says).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis_core.models.ledger import CallRecord

if TYPE_CHECKING:
    from jarvis_core.db.connection import Database
    from jarvis_core.models.factory import AdapterFactory
    from jarvis_proto import Settings

log = logging.getLogger(__name__)

#: range -> (span seconds, bucket seconds). Days are local days.
RANGES: dict[str, tuple[int, int]] = {
    "1h": (3600, 300),
    "6h": (6 * 3600, 900),
    "24h": (86400, 3600),
    "7d": (7 * 86400, 3 * 3600),
    "30d": (30 * 86400, 86400),
    "90d": (90 * 86400, 86400),
}
DAY = 86400
RECENT_S = 15 * 60
PRUNE_EVERY_S = 6 * 3600
OVERFLOW_KINDS = ("rejected", "shrunk", "trimmed", "cut")

_COLUMNS = (
    "id, queued_at, started_at, ended_at, endpoint, model, role, source, label, run_id, conversation_id, "
    "prompt_tokens, completion_tokens, cached_tokens, estimated, ctx_window, outcome, trimmed_tokens, "
    "shrunk, rejected, cut, error"
)


def source_key(source: str, label: str | None) -> str:
    """One line on the screen. Scheduled prompts and housekeeping are split by name; every chat is
    one "chat" (the screen is about load, not about which chat)."""
    return f"{source}:{label}" if label and source in {"scheduled", "housekeeping"} else source


def overflow_kinds(row: Any) -> list[str]:
    kinds = []
    if row["rejected"]:
        kinds.append("rejected")
    if row["shrunk"]:
        kinds.append("shrunk")
    if row["trimmed_tokens"]:
        kinds.append("trimmed")
    if row["cut"]:
        kinds.append("cut")
    return kinds


def bucket_starts(start: datetime, end: datetime, bucket_s: int, tz: ZoneInfo) -> list[datetime]:
    """Bucket boundaries covering [start, end), aligned to the asker's wall clock. Day buckets
    start at local midnights (DST days are 23/25 h long); shorter ones are fixed steps aligned to
    local time at the first boundary."""
    if bucket_s >= DAY:
        local = start.astimezone(tz)
        day = datetime(local.year, local.month, local.day, tzinfo=tz)
        out = []
        while day.astimezone(UTC) < end:
            out.append(day.astimezone(UTC))
            nxt = day.date() + timedelta(days=bucket_s // DAY)
            day = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
        return out
    offset = start.astimezone(tz).utcoffset() or timedelta(0)
    local_epoch = start.timestamp() + offset.total_seconds()
    first = datetime.fromtimestamp(math.floor(local_epoch / bucket_s) * bucket_s - offset.total_seconds(), UTC)
    out = []
    t = first
    while t < end:
        out.append(t)
        t += timedelta(seconds=bucket_s)
    return out


def _union_ms(intervals: list[tuple[int, int]]) -> int:
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None and cur_s is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None and cur_s is not None:
        total += cur_e - cur_s
    return total


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


@dataclass
class _Row:
    raw: Any

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def tokens(self) -> int:
        return int(self.raw["prompt_tokens"]) + int(self.raw["completion_tokens"])

    @property
    def busy(self) -> tuple[int, int] | None:
        s = self.raw["started_at"]
        return (int(s), int(self.raw["ended_at"])) if s is not None and self.raw["ended_at"] >= s else None


class ContextUsage:
    def __init__(self, db: Database, settings: Callable[[], Settings], adapters: AdapterFactory) -> None:
        self.db = db
        self._settings = settings
        self.adapters = adapters
        self.ledger = adapters.ledger
        self.ledger.sink = self.record
        self.ledger.schedule_of = self.schedule_name
        self._task: asyncio.Task[None] | None = None

    # --- writing ------------------------------------------------------------------------

    async def record(self, rec: CallRecord) -> None:
        await self.db.execute(
            f"INSERT OR REPLACE INTO model_calls ({_COLUMNS}) VALUES ({', '.join('?' * 22)})",
            (
                rec.id,
                rec.queued_ms,
                rec.started_ms,
                rec.ended_ms or rec.queued_ms,
                rec.endpoint,
                rec.model,
                rec.role,
                rec.source,
                rec.label,
                rec.run_id,
                rec.conversation_id,
                rec.prompt_tokens,
                rec.completion_tokens,
                rec.cached_tokens,
                int(rec.estimated),
                rec.window,
                rec.outcome,
                rec.trimmed_tokens,
                int(rec.shrunk),
                int(rec.rejected),
                int(rec.cut),
                rec.error,
            ),
        )

    async def schedule_name(self, conversation_id: str) -> str | None:
        row = await self.db.fetchone("SELECT folder_label FROM conversations WHERE id = ?", (conversation_id,))
        return row["folder_label"] if row is not None else None

    async def prune(self, *, now_ms: int | None = None) -> int:
        days = self._settings().context_usage_retention_days
        cutoff = (now_ms or int(time.time() * 1000)) - days * DAY * 1000
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM model_calls WHERE queued_at < ?", (cutoff,))
        n = int(row["n"]) if row is not None else 0
        if n:
            await self.db.execute("DELETE FROM model_calls WHERE queued_at < ?", (cutoff,))
        return n

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="context-usage-prune")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.ledger.drain()

    async def _loop(self) -> None:
        while True:
            try:
                if n := await self.prune():
                    log.info("context usage: pruned %d call rows", n)
            except Exception as exc:
                log.warning("context usage prune failed: %s", exc)
            await asyncio.sleep(PRUNE_EVERY_S)

    # --- reading ------------------------------------------------------------------------

    async def endpoints(self) -> list[dict[str, Any]]:
        """The endpoints the settings point at, with the roles on each and its window."""
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for role, spec in self._settings().roles.items():
            g = groups.setdefault(
                (spec.base_url, spec.model),
                {"endpoint": spec.base_url, "model": spec.model, "roles": [], "window": None, "_spec": spec},
            )
            g["roles"].append(role.value)
        out = []
        for g in groups.values():
            spec = g.pop("_spec")
            try:
                g["window"] = await self.adapters.window_for_spec(spec)
            except Exception:
                g["window"] = None
            out.append(g)
        return out

    async def _rows(self, since_ms: int, until_ms: int) -> list[_Row]:
        rows = await self.db.fetchall(
            f"SELECT {_COLUMNS} FROM model_calls WHERE ended_at >= ? AND queued_at < ? ORDER BY queued_at",
            (since_ms, until_ms),
        )
        return [_Row(r) for r in rows]

    async def live(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms or int(time.time() * 1000)
        endpoints = await self.endpoints()
        by_url = {e["endpoint"]: e for e in endpoints}
        for e in endpoints:
            e.update(calls=[], used=0, waiting=0, recent_peak=0, recent_busy_ms=0, recent_by_source={})
        for rec in list(self.ledger.live.values()):
            e = by_url.get(rec.endpoint)
            if e is None:  # a call to an endpoint the settings no longer name (settings just changed)
                e = {"endpoint": rec.endpoint, "model": rec.model, "roles": [], "window": rec.window}
                e.update(calls=[], used=0, waiting=0, recent_peak=0, recent_busy_ms=0, recent_by_source={})
                by_url[rec.endpoint] = e
                endpoints.append(e)
            live = rec.as_live()
            live["key"] = source_key(rec.source, rec.label)
            e["calls"].append(live)
            if rec.state == "waiting":
                e["waiting"] += 1
            else:
                e["used"] += live["tokens"]
        recent = await self._rows(now - RECENT_S * 1000, now + 1)
        busy: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for r in recent:
            e = by_url.get(r["endpoint"])
            if e is None:
                continue
            key = source_key(r["source"], r["label"])
            e["recent_peak"] = max(e["recent_peak"], r.tokens)
            slot = e["recent_by_source"].setdefault(key, {"peak": 0, "calls": 0})
            slot["peak"] = max(slot["peak"], r.tokens)
            slot["calls"] += 1
            if (b := r.busy) is not None:
                busy[r["endpoint"]].append((max(b[0], now - RECENT_S * 1000), min(b[1], now)))
        for url, iv in busy.items():
            by_url[url]["recent_busy_ms"] = _union_ms([i for i in iv if i[1] > i[0]])
        day = await self._rows(now - DAY * 1000, now + 1)
        counts = dict.fromkeys(OVERFLOW_KINDS, 0)
        last: list[dict[str, Any]] = []
        for r in day:
            kinds = overflow_kinds(r)
            for k in kinds:
                counts[k] += 1
            if kinds:
                last.append(self._overflow_item(r, kinds))
        last.sort(key=lambda o: o["at"], reverse=True)
        return {
            "now": now,
            "recent_s": RECENT_S,
            "endpoints": endpoints,
            "overflows_24h": counts,
            "last_overflows": last[:20],
        }

    @staticmethod
    def _overflow_item(r: _Row, kinds: list[str]) -> dict[str, Any]:
        return {
            "at": int(r["queued_at"]),
            "key": source_key(r["source"], r["label"]),
            "source": r["source"],
            "label": r["label"],
            "role": r["role"],
            "endpoint": r["endpoint"],
            "model": r["model"],
            "kinds": kinds,
            "tokens": r.tokens,
            "window": r["ctx_window"],
            "trimmed_tokens": int(r["trimmed_tokens"]),
            "run_id": r["run_id"],
            "conversation_id": r["conversation_id"],
            "error": r["error"],
        }

    async def usage(self, range_: str, tz_name: str, *, now_ms: int | None = None) -> dict[str, Any]:
        span_s, bucket_s = RANGES[range_]
        tz = ZoneInfo(tz_name)
        now = now_ms or int(time.time() * 1000)
        end = datetime.fromtimestamp(now / 1000, UTC)
        starts = bucket_starts(end - timedelta(seconds=span_s), end, bucket_s, tz)
        edges = [int(s.timestamp() * 1000) for s in starts] + [now]
        # The last bucket ends now; a bucket's length is what busy % is measured against.
        lengths = [max(1, edges[i + 1] - edges[i]) for i in range(len(starts))]
        rows = await self._rows(edges[0], now + 1)
        n = len(starts)

        def bucket_of(t: int) -> int | None:
            if t < edges[0] or t > now:
                return None
            lo, hi = 0, n - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if edges[mid] <= t:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        series: dict[str, dict[str, Any]] = {}
        windows: dict[str, list[int | None]] = {}
        ep_busy: dict[str, list[list[tuple[int, int]]]] = {}
        overflows: list[dict[str, Any]] = []
        table: dict[str, dict[str, Any]] = {}

        def new_series(r: _Row) -> dict[str, Any]:
            return {
                "source": r["source"],
                "label": r["label"],
                "peak": [0] * n,
                "calls": [0] * n,
                "tokens": [0] * n,
                "busy_ms": [0] * n,
                "overflows": [0] * n,
            }

        for r in rows:
            key = source_key(r["source"], r["label"])
            s = series.get(key) or series.setdefault(key, new_series(r))
            t = table.setdefault(
                key,
                {
                    "key": key,
                    "source": r["source"],
                    "label": r["label"],
                    "calls": 0,
                    "failed": 0,
                    "sizes": [],
                    "tokens": 0,
                    "busy_ms": 0,
                    "overflows": dict.fromkeys(OVERFLOW_KINDS, 0),
                    "endpoints": set(),
                    "last_at": 0,
                    "max_window": None,
                },
            )
            b = bucket_of(int(r["queued_at"]))
            kinds = overflow_kinds(r)
            processed = max(0, int(r["prompt_tokens"]) - int(r["cached_tokens"])) + int(r["completion_tokens"])
            if b is not None:
                s["peak"][b] = max(s["peak"][b], r.tokens)
                s["calls"][b] += 1
                s["tokens"][b] += processed
                s["overflows"][b] += 1 if kinds else 0
                w = windows.setdefault(r["endpoint"], [None] * n)
                if r["ctx_window"]:
                    w[b] = max(w[b] or 0, int(r["ctx_window"]))
            if b is not None:  # a call queued before the range counts only its busy time in it
                t["calls"] += 1
                t["failed"] += 1 if r["outcome"] == "failed" else 0
                t["sizes"].append(r.tokens)
                t["tokens"] += processed
                t["endpoints"].add(r["endpoint"])
                t["last_at"] = max(t["last_at"], int(r["queued_at"]))
                if r["ctx_window"]:
                    t["max_window"] = max(t["max_window"] or 0, int(r["ctx_window"]))
                for k in kinds:
                    t["overflows"][k] += 1
                if kinds:
                    overflows.append(self._overflow_item(r, kinds))
            if (iv := r.busy) is not None:
                per = ep_busy.setdefault(r["endpoint"], [[] for _ in range(n)])
                # Spread the call's busy time over the buckets it spans.
                lo = bucket_of(max(iv[0], edges[0]))
                hi = bucket_of(min(iv[1], now))
                if lo is not None and hi is not None:
                    for i in range(lo, hi + 1):
                        a, z = max(iv[0], edges[i]), min(iv[1], edges[i + 1])
                        if z > a:
                            s["busy_ms"][i] += z - a
                            t["busy_ms"] += z - a
                            per[i].append((a, z))
        # Fill windows so a line has no holes where a bucket had no calls: carry the last known.
        for w in windows.values():
            last = next((v for v in w if v), None)
            for i in range(n):
                if w[i]:
                    last = w[i]
                else:
                    w[i] = last
        endpoint_busy = {url: [_union_ms(iv) for iv in per] for url, per in ep_busy.items()}
        rows_table = []
        for t in table.values():
            sizes = t.pop("sizes")
            t["peak"] = max(sizes) if sizes else 0
            t["p50"] = _percentile(sizes, 0.5)
            t["p95"] = _percentile(sizes, 0.95)
            t["endpoints"] = sorted(t["endpoints"])
            rows_table.append(t)
        rows_table.sort(key=lambda t: t["busy_ms"], reverse=True)
        overflows.sort(key=lambda o: o["at"], reverse=True)
        return {
            "range": range_,
            "tz": tz_name,
            "now": now,
            "bucket_s": bucket_s,
            "buckets": edges[:-1],
            "lengths": lengths,
            "series": series,
            "windows": windows,
            "endpoint_busy": endpoint_busy,
            "overflows": overflows[:200],
            "overflow_count": len(overflows),
            "table": rows_table,
            "hour_profile": await self.hour_profile(tz, now=now),
            "endpoints": await self.endpoints(),
        }

    async def hour_profile(self, tz: ZoneInfo, *, now: int, days: int = 7) -> dict[str, Any]:
        """Busy minutes per local hour of day, averaged over the last ``days`` days, per source:
        the hours a moved schedule would collide with."""
        since = now - days * DAY * 1000
        rows = await self._rows(since, now + 1)
        prof: dict[str, list[float]] = {}
        for r in rows:
            iv = r.busy
            if iv is None:
                continue
            key = source_key(r["source"], r["label"])
            bins = prof.setdefault(key, [0.0] * 24)
            a, z = max(iv[0], since), min(iv[1], now)
            while a < z:
                local = datetime.fromtimestamp(a / 1000, UTC).astimezone(tz)
                nxt_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                nxt = min(z, int(nxt_local.timestamp() * 1000))
                if nxt <= a:  # guard: never loop on a clock oddity
                    nxt = z
                bins[local.hour] += (nxt - a) / 60000.0
                a = nxt
        for key, bins in prof.items():
            prof[key] = [round(v / days, 2) for v in bins]
        return {"days": days, "busy_min_per_day": prof}
