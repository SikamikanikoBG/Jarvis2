"""Web search through SearXNG (the instance on ardi), as a tool.

V1 searched by shelling out to curl against SearXNG; V2 had only `fetch`, which respects
robots.txt - so "World news" hit Google News three times and the supervisor stopped it
(2026-09-05). Search is a capability, not a shell trick: this tool returns ranked results with
snippets, and `fetch.fetch` reads the pages the model picks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Settings, ToolResult

log = logging.getLogger(__name__)


class _SearchArgs(BaseModel):
    query: str = Field(description="Search query (any language)")
    max_results: int = Field(default=8, ge=1, le=20)
    time_range: str | None = Field(
        default=None, description="Restrict recency: 'day', 'week', 'month' or 'year'. Omit for no restriction."
    )
    language: str | None = Field(default=None, description="Result language, e.g. 'bg', 'en'. Omit for all.")
    categories: str | None = Field(default=None, description="SearXNG categories, e.g. 'news' or 'general'.")


class WebTools(BuiltinProvider):
    name = "web"
    #: The search backend is outside the core: listed in "What you can reach now" (features/tasks.py).
    external = True

    def __init__(self, settings: Callable[[], Settings], client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(25.0))
        # Why search cannot answer right now, from SearXNG's own report - None while it does. The
        # registry reads it after each call, so "web search is not working" is a status, not a
        # string of empty results the model searches through for two minutes (alpha F-10).
        self._error: str | None = None
        super().__init__()

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def configured(self) -> bool:
        """A search engine to ask. Without one, ``web.search`` is not offered (tools/registry.py)."""
        return bool((self._settings().searxng_url or "").strip())

    async def aclose(self) -> None:
        await self._client.aclose()

    @tool(
        "web.search",
        description=(
            "Search the web (SearXNG: many engines, no tracking). Returns ranked results with title, url, "
            "snippet and date. Use it before fetch.fetch to find the right pages; for news use "
            "categories='news' and time_range='day'."
        ),
        args=_SearchArgs,
        read_only=True,
        idempotent=True,
    )
    async def _search(
        self,
        query: str,
        max_results: int = 8,
        time_range: str | None = None,
        language: str | None = None,
        categories: str | None = None,
    ) -> ToolResult:
        base = (self._settings().searxng_url or "").rstrip("/")
        if not base:
            return ToolResult.failure("no search engine configured (settings.searxng_url)")
        params: dict[str, Any] = {"q": query, "format": "json"}
        if time_range in {"day", "week", "month", "year"}:
            params["time_range"] = time_range
        if language:
            params["language"] = language
        if categories:
            params["categories"] = categories
        try:
            resp = await self._client.get(f"{base}/search", params=params)
        except httpx.HTTPError as exc:
            return self._down(f"search backend unreachable: {type(exc).__name__}: {exc}")
        if resp.status_code >= 400:
            return self._down(f"search backend HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError:
            return self._down("search backend returned non-JSON")
        results = [r for r in data.get("results", []) if isinstance(r, dict) and r.get("url")][:max_results]
        silent: list[list[Any]] = [list(e) for e in data.get("unresponsive_engines", []) if isinstance(e, list) and e]
        if not results and silent:
            # Nothing found because the engines behind SearXNG did not answer (suspended, rate
            # limited) - not because nothing exists. Said as such, and marked as down.
            why = ", ".join(" ".join(str(x) for x in e[:2]) for e in silent[:5])
            return self._down(f"the search engines are not answering: {why}")
        self._error = None
        if not results:
            hint = "; ".join(str(s) for s in data.get("suggestions", [])[:3])
            return ToolResult.empty(f"No results for {query!r}." + (f" Suggestions: {hint}" if hint else ""))
        lines = []
        for i, r in enumerate(results, 1):
            host = urlparse(str(r["url"])).netloc
            date = str(r.get("publishedDate") or "")[:10]
            snippet = " ".join(str(r.get("content") or "").split())[:280]
            lines.append(
                f"{i}. {str(r.get('title') or '').strip()[:120]}  [{host}{(' ' + date) if date else ''}]\n"
                f"   {r['url']}\n   {snippet}"
            )
        return ToolResult.data(
            "\n".join(lines), count=len(results), total=int(data.get("number_of_results") or 0) or None
        )

    def _down(self, why: str) -> ToolResult:
        self._error = why
        return ToolResult.failure(why)
