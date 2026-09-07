"""Tool providers and the registry the loop calls into.

Phase 1 exposes tools flat. Derived facades per namespace (DESIGN §6) arrive with the MCP
provider in Phase 3 — the registry is the only place that will change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any, Protocol

import jsonschema

from jarvis_proto import ToolResult, ToolSpec

log = logging.getLogger(__name__)

_NAMES_IN_ERROR = 40  # enough for any one namespace; a wall of text helps nobody


class ToolProvider(Protocol):
    name: str

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        timeout_s: float,
    ) -> ToolResult: ...


class ToolMemory(Protocol):
    """Where the last-known tool sets survive a restart (``Store`` implements it)."""

    async def load_provider_tools(self) -> dict[str, list[ToolSpec]]: ...

    async def save_provider_tools(self, provider: str, specs: list[ToolSpec]) -> None: ...


class ToolRegistry:
    def __init__(self, providers: list[ToolProvider] | None = None, *, memory: ToolMemory | None = None) -> None:
        self._providers: list[ToolProvider] = []
        self._index: dict[str, tuple[ToolProvider, ToolSpec]] = {}
        self._errors: dict[str, str] = {}
        # The last tool set each provider was known to have. The model-facing list must describe
        # what Jarvis can DO, not what happens to be reachable this second: the tool list renders
        # into the system prompt, so a provider dropping out re-prefills every conversation
        # (measured 2026-09-06: the browser extension alone, 71<->73 tools, cost 17 s a turn).
        self._last_known: dict[str, list[ToolSpec]] = {}
        # ...and it outlives the process, or a restart while the laptop sleeps erases everything
        # that laptop can do. See migration 0007.
        self._memory = memory
        # Called with the provider name when its tool set actually changed (the UI refreshes).
        self._on_change: list[Callable[[str], None]] = []
        self.set_providers(list(providers or []))  # one place installs the reconnect hooks

    async def load_memory(self) -> None:
        """Read the remembered tool sets. Call once, before the first ``refresh``."""
        if self._memory is None:
            return
        try:
            remembered = await self._memory.load_provider_tools()
        except Exception as exc:
            log.warning("could not load the remembered tool sets: %s", exc)
            return
        # Entries for providers that are no longer configured are inert: only a provider that is
        # actually present can put remembered tools back into the index.
        self._last_known = {**remembered, **self._last_known}
        if remembered:
            log.info("remembered tools for %s", ", ".join(f"{n} ({len(s)})" for n, s in remembered.items()))

    def on_change(self, hook: Callable[[str], None]) -> None:
        self._on_change.append(hook)

    def add(self, provider: ToolProvider) -> None:
        self.set_providers([*self._providers, provider])

    def set_providers(self, providers: list[ToolProvider]) -> None:
        self._providers = list(providers)
        names = {p.name for p in self._providers}
        # A provider removed from settings is gone for good; only its memory should outlive a
        # dropped *connection*.
        self._last_known = {n: specs for n, specs in self._last_known.items() if n in names}
        for provider in self._providers:
            # A provider that can reconnect (an MCP server) re-lists itself when it does, so a
            # restarted host does not stay invisible until someone reloads by hand.
            if hasattr(provider, "on_connect"):
                provider.on_connect = self.reindex  # type: ignore[attr-defined]

    async def _resolve(self, provider: ToolProvider, specs: list[ToolSpec] | None, error: str | None) -> list[ToolSpec]:
        """What this provider contributes to the prompt, given what it just answered.

        A listing that succeeded with tools is the truth and replaces the memory (a tool that
        really went away is dropped). A failed listing, or an empty one from a provider that is
        telling us it is not connected, is an OUTAGE — the tools we knew stay in the prompt and
        calling one returns that provider's own "not connected" error, which the loop already
        handles. A provider that genuinely has no tools and no error keeps none.
        """
        if specs:
            if self._last_known.get(provider.name) != specs:
                self._last_known[provider.name] = list(specs)
                await self._remember(provider.name, list(specs))  # only on a real change: this runs every 30 s
            return list(specs)
        if error is not None:
            return list(self._last_known.get(provider.name, []))
        self._last_known.pop(provider.name, None)
        await self._remember(provider.name, [])
        return []

    async def _remember(self, name: str, specs: list[ToolSpec]) -> None:
        if self._memory is None:
            return
        with contextlib.suppress(Exception):  # a write failure must never break tool listing
            await self._memory.save_provider_tools(name, specs)

    async def reindex(self, provider: ToolProvider) -> None:
        """Replace one provider's tools in the index. Safe to call at any time."""
        if provider not in self._providers:
            return
        before = sorted(n for n, (p, _) in self._index.items() if p is provider)
        try:
            listed: list[ToolSpec] | None = await provider.list_tools()
            error = getattr(provider, "error", None)
        except Exception as exc:
            listed, error = None, getattr(provider, "error", None) or f"{type(exc).__name__}: {exc}"
            log.warning("provider %s failed to re-list tools: %s", provider.name, error)
        specs = await self._resolve(provider, listed, error)
        if error is None:
            self._errors.pop(provider.name, None)
        else:
            self._errors[provider.name] = error
        index = {name: entry for name, entry in self._index.items() if entry[0] is not provider}
        for spec in specs:
            index[spec.name] = (provider, spec)
        self._index = index
        after = sorted(s.name for s in specs)
        if before != after:
            log.info("provider %s re-listed: %d tools (was %d)", provider.name, len(after), len(before))
            for hook in self._on_change:
                hook(provider.name)

    def mark_unavailable(self, provider: ToolProvider, error: str | None = None) -> None:
        """A connection went away. Synchronous, and it KEEPS the tools.

        Dropping them here is what made the browser extension's two tools leave the system
        prompt every time Arsen closed his browser, re-prefilling every conversation. The
        provider's own call path already answers "not connected" for anything invoked meanwhile.
        """
        self._errors[provider.name] = error or getattr(provider, "error", None) or "not connected"

    def forget(self, provider: ToolProvider) -> None:
        """Drop a provider's tools and its memory (the provider itself is going away)."""
        self._index = {name: entry for name, entry in self._index.items() if entry[0] is not provider}
        self._last_known.pop(provider.name, None)

    async def refresh(self) -> None:
        index: dict[str, tuple[ToolProvider, ToolSpec]] = {}
        self._errors = {}
        for provider in self._providers:
            try:
                listed: list[ToolSpec] | None = await provider.list_tools()
                error = getattr(provider, "error", None)
            except Exception as exc:
                listed, error = None, getattr(provider, "error", None) or f"{type(exc).__name__}: {exc}"
                log.warning("provider %s failed to list tools: %s", provider.name, error)
            if error is not None:
                self._errors[provider.name] = error
            for spec in await self._resolve(provider, listed, error):
                if spec.name in index:
                    log.warning("tool %s from %s shadows an earlier provider", spec.name, provider.name)
                index[spec.name] = (provider, spec)
        self._index = index

    def provider_health(self) -> list[dict[str, Any]]:
        """Per provider: how many tools it contributes and why it failed, if it did."""
        errors = self._errors
        out: list[dict[str, Any]] = []
        for provider in self._providers:
            count = sum(1 for p, _ in self._index.values() if p is provider)
            error = errors.get(provider.name) or (getattr(provider, "error", None) if count == 0 else None)
            out.append({"name": provider.name, "tools": count, "ok": error is None, "error": error})
        return out

    def specs(self) -> list[ToolSpec]:
        """Sorted by name, always.

        The chat template renders this list into the system message, so the prompt prefix — and
        with it every cached conversation — depends on the ORDER as well as the set. Dict order
        follows registration history: `reindex` re-appends a provider's tools at the end, so a
        reconnect alone reordered the prompt and cost a full re-prefill. Sorting makes the
        rendering a function of the set and nothing else.
        """
        return sorted((spec for _, spec in self._index.values()), key=lambda s: s.name)

    def get(self, name: str) -> ToolSpec | None:
        entry = self._index.get(name)
        return entry[1] if entry else None

    def _cannot_route(self, name: str) -> str:
        """Why a call cannot be routed. An unreachable machine is not a missing capability.

        2026-09-07: jarvis-host died overnight and the core restarted with the laptop
        unreachable, so ``workocholic.outlook_send`` came back as "unknown tool". The run told
        Arsen it could not send his news digest and then guessed two tool names that never
        existed. "unknown tool" has to mean the tool does not exist — anything else is a lie the
        model passes on to him.
        """
        namespace = name.split(".", 1)[0]
        if namespace not in {p.name for p in self._providers}:
            return f"unknown tool {name!r}"
        known = sorted(s.name for s in self._last_known.get(namespace, []))
        if known and name not in known:
            # We know this provider's real tool set, so the name is wrong whether it is reachable
            # or not. Naming the alternatives is what stops the next guess.
            listed = ", ".join(known[:_NAMES_IN_ERROR])
            more = f" (+{len(known) - _NAMES_IN_ERROR} more)" if len(known) > _NAMES_IN_ERROR else ""
            return f"unknown tool {name!r}; {namespace} offers: {listed}{more}"
        error = self._errors.get(namespace)
        if error is not None:
            return f"{namespace} is not reachable right now ({error}); its tools cannot be used until it is back"
        return f"unknown tool {name!r}"

    def validate(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Return an error string if ``arguments`` do not fit the tool's schema."""
        spec = self.get(name)
        if spec is None:
            return self._cannot_route(name)
        if "__raw__" in arguments:
            return f"arguments for {name} were not valid JSON: {arguments['__raw__'][:200]!r}"
        try:
            jsonschema.validate(arguments, spec.input_schema)
        except jsonschema.ValidationError as exc:
            return f"invalid arguments for {name}: {exc.message}"
        return None

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: asyncio.Event,
        idempotency_key: str,
        timeout_s: float = 60.0,
    ) -> ToolResult:
        entry = self._index.get(name)
        if entry is None:
            return ToolResult.failure(self._cannot_route(name))
        provider, _spec = entry
        try:
            return await asyncio.wait_for(
                provider.call(name, arguments, cancel=cancel, idempotency_key=idempotency_key, timeout_s=timeout_s),
                timeout=timeout_s,
            )
        except TimeoutError:
            return ToolResult.failure(f"{name} timed out after {timeout_s:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("tool %s raised", name)
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
