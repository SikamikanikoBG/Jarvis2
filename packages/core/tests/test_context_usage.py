"""Context ledger + Context screen data (idea-to-uat/context-usage): every real model call is
booked live and as a row, attributed to who it was for, with overflows told apart; the history
is bucketed in the asker's clock."""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.features.context_usage import bucket_starts, source_key
from jarvis_core.models.base import VOICE, ModelError, call_urgency, reset_endpoint_semaphores
from jarvis_core.models.factory import current_run_kind
from jarvis_core.models.ledger import CallRecord, call_purpose, is_context_overflow, pending_trim
from jarvis_core.models.ollama import OllamaAdapter
from jarvis_core.models.openai_compat import OpenAICompatAdapter
from jarvis_proto import ConversationKind, Message, ModelSpec, Provider, RunKind
from tests.conftest import Harness

URL = "http://gpu:18100/v1"


def _sse(*objs: dict) -> bytes:
    return ("\n".join(["data: " + json.dumps(o) for o in objs] + ["data: [DONE]"]) + "\n").encode()


def _ok_body(prompt: int = 1200, completion: int = 30, finish: str = "stop", cached: int = 1000) -> bytes:
    return _sse(
        {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": finish}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        },
    )


def _adapter(h: Harness, handler, *, role: str = "chat", **spec) -> OpenAICompatAdapter:  # type: ignore[no-untyped-def]
    s = ModelSpec(provider=Provider.VLLM, base_url=URL, model="flash", num_ctx=98304, **spec)
    a = OpenAICompatAdapter(s, role=role, ledger=h.core.adapters.ledger)
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


async def _run(adapter, text: str = "hi") -> list:  # type: ignore[no-untyped-def]
    return [c async for c in adapter.stream([Message.user(text)], [], cancel=asyncio.Event())]


async def _rows(h: Harness) -> list:
    await h.core.adapters.ledger.drain()
    return await h.core.db.fetchall("SELECT * FROM model_calls ORDER BY queued_at")


@pytest.fixture
async def h(tmp_path: Path):
    reset_endpoint_semaphores()
    harness = Harness(tmp_path)
    await harness.start()
    try:
        yield harness
    finally:
        await harness.stop()


async def test_a_finished_call_becomes_one_row_with_its_usage_and_window(h: Harness):
    a = _adapter(h, lambda r: httpx.Response(200, content=_ok_body()))
    await _run(a)
    rows = await _rows(h)
    assert len(rows) == 1
    r = rows[0]
    assert (r["endpoint"], r["model"], r["role"], r["ctx_window"]) == (URL, "flash", "chat", 98304)
    assert (r["prompt_tokens"], r["completion_tokens"], r["cached_tokens"]) == (1200, 30, 1000)
    assert r["outcome"] == "ok" and not r["estimated"]
    assert r["started_at"] is not None and r["ended_at"] >= r["started_at"] >= r["queued_at"]
    # Outside any run and with no purpose: housekeeping, named by the role.
    assert (r["source"], r["label"]) == ("housekeeping", "chat")
    assert h.core.adapters.ledger.live == {}


async def test_a_call_in_flight_is_live_with_a_growing_estimate(h: Harness):
    gate = asyncio.Event()

    async def body():  # type: ignore[no-untyped-def]
        yield b'data: {"choices":[{"delta":{"content":"' + b"x" * 320 + b'"}}]}\n'
        await gate.wait()
        yield _ok_body(prompt=5000, completion=100)

    a = _adapter(h, lambda r: httpx.Response(200, content=body()))
    task = asyncio.create_task(_run(a, "y" * 3200))
    for _ in range(100):
        await asyncio.sleep(0.01)
        live = await h.core.context_usage.live()
        calls = [c for e in live["endpoints"] for c in e["calls"]]
        if calls and calls[0]["tokens"] > 1000:
            break
    assert calls and calls[0]["state"] == "running" and calls[0]["window"] == 98304
    assert calls[0]["tokens"] >= 1000 + 100  # ~3200 chars of prompt + 320 streamed, at 3.2 chars/token
    ep = next(e for e in live["endpoints"] if e["calls"])
    assert ep["used"] == calls[0]["tokens"]
    gate.set()
    await task
    assert h.core.adapters.ledger.live == {}


async def test_a_call_waiting_for_its_endpoint_shows_as_waiting(h: Harness):
    h.core.apply_settings(h.core.settings.model_copy(update={"max_concurrent_runs_per_endpoint": 1}))
    gate = asyncio.Event()

    async def slow():  # type: ignore[no-untyped-def]
        await gate.wait()
        yield _ok_body()

    first = _adapter(h, lambda r: httpx.Response(200, content=slow()))
    second = OpenAICompatAdapter(first.spec, concurrency=1, role="planner", ledger=h.core.adapters.ledger)
    first._sem = second._sem  # one endpoint, one slot
    second._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_ok_body())))
    t1 = asyncio.create_task(_run(first))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(_run(second))
    await asyncio.sleep(0.05)
    states = sorted(c.state for c in h.core.adapters.ledger.live.values())
    assert states == ["running", "waiting"]
    gate.set()
    await asyncio.gather(t1, t2)
    assert len(await _rows(h)) == 2


async def test_llama_cpp_context_refusal_is_a_rejected_overflow(h: Harness):
    refusal = {
        "error": {
            "code": 400,
            "message": "the request exceeds the available context size, try increasing it",
            "type": "exceed_context_size_error",
        }
    }
    a = _adapter(h, lambda r: httpx.Response(400, content=json.dumps(refusal).encode()))
    with pytest.raises(ModelError):
        await _run(a)
    (r,) = await _rows(h)
    assert r["outcome"] == "failed" and r["rejected"] == 1 and r["estimated"] == 1
    assert "exceeds the available context" in r["error"]


async def test_a_500_mid_generation_context_full_is_rejected_too():
    assert is_context_overflow('HTTP 500: {"error":{"message":"Context size has been exceeded."}}')
    assert is_context_overflow("This model's maximum context length is 65536 tokens.")
    assert not is_context_overflow("HTTP 500: CUDA out of memory")


async def test_vllm_too_long_400_then_retry_is_a_shrunk_call(h: Harness):
    refusal = (
        '{"error":{"message":"This model\'s maximum context length is 65536 tokens. However, you requested '
        "16384 output tokens and your prompt contains at least 49153 input tokens, for a total of at least "
        '65537 tokens."}}'
    )
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return (
            httpx.Response(400, content=refusal.encode()) if len(seen) == 1 else httpx.Response(200, content=_ok_body())
        )

    a = _adapter(h, handler, max_tokens=16384)
    await _run(a)
    (r,) = await _rows(h)
    assert r["outcome"] == "ok" and r["shrunk"] == 1 and r["rejected"] == 0


async def test_an_answer_stopped_at_the_output_limit_is_cut(h: Harness):
    a = _adapter(h, lambda r: httpx.Response(200, content=_ok_body(finish="length")))
    await _run(a)
    (r,) = await _rows(h)
    assert r["cut"] == 1


async def test_a_caller_that_stops_early_books_a_cancelled_call_and_frees_the_slot(h: Harness):
    a = _adapter(h, lambda r: httpx.Response(200, content=_ok_body()))
    agen = a.stream([Message.user("hi")], [], cancel=asyncio.Event())
    await agen.__anext__()
    await agen.aclose()
    (r,) = await _rows(h)
    assert r["outcome"] == "cancelled"
    assert a._sem.busy == 0


async def test_a_server_without_usage_is_estimated_from_chars(h: Harness):
    body = _sse({"choices": [{"delta": {"content": "a" * 64}, "finish_reason": "stop"}]})
    a = _adapter(h, lambda r: httpx.Response(200, content=body))
    await _run(a, "q" * 640)
    (r,) = await _rows(h)
    assert r["estimated"] == 1 and r["prompt_tokens"] >= 200 and r["completion_tokens"] == 20


async def test_ollama_calls_are_booked_too(h: Harness):
    spec = ModelSpec(provider=Provider.OLLAMA, base_url="http://ollama:11434", model="q", num_ctx=32768)
    a = OllamaAdapter(spec, role="background", ledger=h.core.adapters.ledger)
    lines = [
        json.dumps({"message": {"content": "ok"}}),
        json.dumps({"done": True, "done_reason": "stop", "prompt_eval_count": 900, "eval_count": 9}),
    ]
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content="\n".join(lines).encode()))
    )
    await _run(a)
    (r,) = await _rows(h)
    assert (r["prompt_tokens"], r["completion_tokens"], r["ctx_window"], r["role"]) == (900, 9, 32768, "background")


async def test_attribution_scheduled_by_name_voice_and_purpose(h: Harness):
    conv = await h.core.store.create_conversation(
        kind=ConversationKind.SCHEDULED, title="Morning brief · 2026-11-07 05:00"
    )
    await h.core.db.execute(
        "UPDATE conversations SET folder_key = 's1', folder_label = 'Morning brief' WHERE id = ?", (conv.id,)
    )
    a = _adapter(h, lambda r: httpx.Response(200, content=_ok_body()), role="planner")

    async def scheduled() -> None:
        current_run_kind.set(RunKind.SCHEDULED)
        current_run_id.set("run_s")
        current_conversation_id.set(conv.id)
        await _run(a)

    async def voice() -> None:
        current_run_kind.set(RunKind.CHAT)
        call_urgency.set(VOICE)
        await _run(a)

    async def journal_inside_a_run() -> None:
        current_run_kind.set(RunKind.CHAT)
        call_purpose.set("journal")
        await _run(a)

    for job in (scheduled, voice, journal_inside_a_run):
        await asyncio.create_task(job())
    rows = await _rows(h)
    got = [(r["source"], r["label"], r["role"]) for r in rows]
    assert got == [
        ("scheduled", "Morning brief", "planner"),
        ("voice", None, "planner"),
        ("housekeeping", "journal", "planner"),
    ]
    assert rows[0]["run_id"] == "run_s" and rows[0]["conversation_id"] == conv.id
    assert source_key("scheduled", "Morning brief") == "scheduled:Morning brief"
    assert source_key("chat", None) == "chat"


async def test_history_trimmed_before_a_call_is_carried_by_that_call_only(h: Harness):
    a = _adapter(h, lambda r: httpx.Response(200, content=_ok_body()))

    async def turn() -> None:
        pending_trim.set(4200)
        await _run(a)
        await _run(a)

    await asyncio.create_task(turn())
    rows = await _rows(h)
    assert [r["trimmed_tokens"] for r in rows] == [4200, 0]


async def test_a_ledger_that_cannot_write_never_fails_the_call(h: Harness):
    async def broken(rec: CallRecord) -> None:
        raise RuntimeError("disk full")

    h.core.adapters.ledger.sink = broken
    a = _adapter(h, lambda r: httpx.Response(200, content=_ok_body()))
    events = await _run(a)
    assert any(getattr(e, "text", None) == "hello" for e in events)
    await h.core.adapters.ledger.drain()


# --- history ---------------------------------------------------------------------------

T0 = int(datetime(2026, 11, 6, 10, 0, tzinfo=UTC).timestamp() * 1000)  # 12:00 Sofia


async def _insert(h: Harness, **kw) -> None:  # type: ignore[no-untyped-def]
    rec = CallRecord(
        id=kw.pop("id"),
        endpoint=kw.pop("endpoint", URL),
        model="flash",
        role=kw.pop("role", "chat"),
        source=kw.pop("source", "chat"),
        label=kw.pop("label", None),
        run_id=None,
        conversation_id=None,
        window=kw.pop("window", 98304),
        queued_ms=kw.pop("at"),
    )
    rec.started_ms = rec.queued_ms + kw.pop("wait", 0)
    rec.ended_ms = rec.started_ms + kw.pop("dur", 10_000)
    rec.outcome = kw.pop("outcome", "ok")
    for k, v in kw.items():
        setattr(rec, k, v)
    await h.core.context_usage.record(rec)


async def test_usage_buckets_peaks_busy_overflows_and_table(h: Harness):
    hour = 3600_000
    await _insert(h, id="a", at=T0 - 3 * hour, prompt_tokens=20_000, completion_tokens=500)
    await _insert(h, id="b", at=T0 - 3 * hour + 5_000, prompt_tokens=60_000, completion_tokens=1000, cut=True)
    await _insert(
        h,
        id="c",
        at=T0 - 2 * hour,
        source="scheduled",
        label="Morning brief",
        prompt_tokens=90_000,
        rejected=True,
        outcome="failed",
    )
    # Spans the boundary between two hourly buckets: its busy time is split between them.
    await _insert(
        h,
        id="d",
        at=T0 - hour - 30 * 60_000,
        dur=3600_000,
        source="scheduled",
        label="Morning brief",
        prompt_tokens=30_000,
        trimmed_tokens=8000,
    )
    u = await h.core.context_usage.usage("24h", "Europe/Sofia", now_ms=T0)
    assert u["bucket_s"] == 3600 and len(u["buckets"]) in (24, 25)
    chat = u["series"]["chat"]
    i = u["buckets"].index(T0 - 3 * hour)
    assert chat["peak"][i] == 61_000 and chat["calls"][i] == 2 and chat["overflows"][i] == 1
    sched = u["series"]["scheduled:Morning brief"]
    j = u["buckets"].index(T0 - 2 * hour)
    assert sched["peak"][j] == 90_000
    assert sched["busy_ms"][j] == 30 * 60_000 + 10_000 and sched["busy_ms"][j + 1] == 30 * 60_000
    # The window line has no holes.
    assert all(w == 98304 for w in u["windows"][URL][i:])
    row = next(t for t in u["table"] if t["key"] == "scheduled:Morning brief")
    assert row["calls"] == 2 and row["peak"] == 90_000 and row["failed"] == 1
    assert row["overflows"] == {"rejected": 1, "shrunk": 0, "trimmed": 1, "cut": 0}
    assert u["overflow_count"] == 3 and {o["kinds"][0] for o in u["overflows"]} == {"cut", "rejected", "trimmed"}
    # Two chat calls overlap: the endpoint was busy for their union, not their sum.
    assert u["endpoint_busy"][URL][i] == 15_000
    # 12:00 Sofia = hour 12; the profile is minutes per day over 7 days.
    prof = u["hour_profile"]["busy_min_per_day"]["chat"]
    assert prof[9] == pytest.approx((10_000 + 10_000) / 60000 / 7, abs=0.01)


async def test_usage_with_no_data_is_empty_not_an_error(h: Harness):
    u = await h.core.context_usage.usage("7d", "Europe/Sofia", now_ms=T0)
    assert u["series"] == {} and u["table"] == [] and u["overflow_count"] == 0 and len(u["buckets"]) >= 56


def test_day_buckets_are_local_midnights_across_dst():
    tz = ZoneInfo("Europe/Sofia")
    start = datetime(2026, 10, 23, 12, tzinfo=UTC)
    end = datetime(2026, 10, 28, 12, tzinfo=UTC)
    starts = bucket_starts(start, end, 86400, tz)
    local = [s.astimezone(tz) for s in starts]
    assert all((d.hour, d.minute) == (0, 0) for d in local)
    lengths = [(b - a).total_seconds() / 3600 for a, b in itertools.pairwise(starts)]
    assert 25.0 in lengths  # 2026-10-25, the clocks went back
    assert set(lengths) <= {24.0, 25.0}


def test_three_hour_buckets_align_to_the_local_clock():
    tz = ZoneInfo("Europe/Sofia")
    starts = bucket_starts(datetime(2026, 11, 6, 7, 40, tzinfo=UTC), datetime(2026, 11, 7, tzinfo=UTC), 3 * 3600, tz)
    assert starts[0].astimezone(tz).hour % 3 == 0 and starts[0].astimezone(tz).minute == 0


async def test_old_rows_are_pruned_after_the_retention(h: Harness):
    day = 86_400_000
    await _insert(h, id="old", at=T0 - 91 * day)
    await _insert(h, id="new", at=T0 - 89 * day)
    assert await h.core.context_usage.prune(now_ms=T0) == 1
    assert [r["id"] for r in await _rows(h)] == ["new"]


async def test_live_lists_every_configured_endpoint_and_last_day_overflows(h: Harness):
    import time

    now = int(time.time() * 1000)
    await _insert(h, id="x", at=now - 60_000, source="triage", rejected=True, outcome="failed", prompt_tokens=99_000)
    live = await h.core.context_usage.live(now_ms=now)
    assert live["endpoints"], "the configured endpoints are listed even when idle"
    assert live["overflows_24h"]["rejected"] == 1
    assert live["last_overflows"][0]["key"] == "triage" and live["last_overflows"][0]["tokens"] == 99_000


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        yield c


def test_api_validates_range_and_timezone(client: TestClient):
    h = {"Authorization": "Bearer secret"}
    assert client.get("/api/context/live").status_code == 401
    assert client.get("/api/context/live", headers=h).status_code == 200
    assert client.get("/api/context/usage?range=2y", headers=h).status_code == 400
    assert client.get("/api/context/usage?range=24h&tz=Mars/Olympus", headers=h).status_code == 400
    body = client.get("/api/context/usage?range=24h&tz=Europe/Sofia", headers=h).json()
    assert body["range"] == "24h" and body["tz"] == "Europe/Sofia" and "hour_profile" in body
