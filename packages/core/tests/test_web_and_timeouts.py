"""web.search through a mocked SearXNG, and the per-call tool timeout honouring timeout_s."""

from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import BaseModel

from jarvis_core.features.web import WebTools
from jarvis_core.models.fake import FakeTurn
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import ToolCall, ToolResult
from tests.conftest import Harness


async def test_web_search_formats_results_and_reports_failures(harness: Harness):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "empty" in str(request.url):
            return httpx.Response(200, json={"results": [], "suggestions": ["something else"]})
        return httpx.Response(
            200,
            json={
                "number_of_results": 1234,
                "results": [
                    {
                        "title": "Дневник: новини",
                        "url": "https://www.dnevnik.bg/a/1",
                        "content": "Първа новина   с   много  интервали",
                        "publishedDate": "2026-09-05T06:00:00",
                        "engine": "bing news",
                    },
                    {"title": "Second", "url": "https://example.org/x", "content": "y"},
                ],
            },
        )

    web = WebTools(lambda: harness.core.settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    res = await web.call("web.search", {"query": "x"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    assert res.kind.value == "error" and "searxng_url" in res.text  # unconfigured → honest error

    harness.enable(searxng_url="http://searx.local:8085")
    res = await web.call(
        "web.search",
        {"query": "новини България", "time_range": "day", "categories": "news", "max_results": 5},
        cancel=asyncio.Event(),
        idempotency_key="k",
        timeout_s=5,
    )
    assert res.kind.value == "data" and res.count == 2 and res.total == 1234
    assert "1. Дневник: новини  [www.dnevnik.bg 2026-09-05]" in res.text
    assert "https://www.dnevnik.bg/a/1" in res.text and "Първа новина с много интервали" in res.text
    assert "format=json" in seen[-1] and "time_range=day" in seen[-1] and "categories=news" in seen[-1]

    res = await web.call("web.search", {"query": "empty"}, cancel=asyncio.Event(), idempotency_key="k", timeout_s=5)
    assert res.kind.value == "empty" and "something else" in res.text


class _SlowArgs(BaseModel):
    timeout_s: int = 60


async def test_tool_timeout_honours_the_tools_own_timeout_s(harness: Harness):
    """A shell_run asking for timeout_s=8 must not be cut at the 120 s default's smaller
    test stand-in - and must be capped by settings.tool_timeout_max_s."""

    class Slow(BuiltinProvider):
        name = "slow"

        @tool("slow.run", description="sleeps", args=_SlowArgs, read_only=True)
        async def _run(self, timeout_s: int = 60) -> ToolResult:
            await asyncio.sleep(1.5)
            return ToolResult.data(f"finished after asking for {timeout_s}s")

    harness.core.registry.add(Slow())
    await harness.core.registry.refresh()

    # Cap below the tool's runtime: the core cuts it and says so.
    harness.enable(tool_timeout_max_s=1)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c1", name="slow.run", arguments={"timeout_s": 30})]),
        FakeTurn(text="ok"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="go", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    tr = next(e for e in seen if e.type == "tool.result")
    assert tr.result.kind.value == "error" and "timed out after 1s" in tr.result.text

    # Cap above it: the tool's own timeout_s (30) is honoured and the call finishes.
    harness.enable(tool_timeout_max_s=600)
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="c2", name="slow.run", arguments={"timeout_s": 30})]),
        FakeTurn(text="ok"),
    )
    await harness.core.engine.create_run(text="again", conversation_id=conv.id)
    seen = await harness.wait_for(sub, "run.done", timeout=10)
    tr = next(e for e in seen if e.type == "tool.result")
    assert tr.result.kind.value == "data" and "finished after asking for 30s" in tr.result.text
    assert json.loads(json.dumps(tr.result.model_dump()))["kind"] == "data"
