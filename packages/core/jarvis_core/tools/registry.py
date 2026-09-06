"""Tool providers and the registry the loop calls into.

Phase 1 exposes tools flat. Derived facades per namespace (DESIGN §6) arrive with the MCP
provider in Phase 3 — the registry is the only place that will change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

import jsonschema

from jarvis_proto import ToolResult, ToolSpec

log = logging.getLogger(__name__)


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


class ToolRegistry:
    def __init__(self, providers: list[ToolProvider] | None = None) -> None:
        self._providers: list[ToolProvider] = []
        self._index: dict[str, tuple[ToolProvider, ToolSpec]] = {}
        self._errors: dict[str, str] = {}
        # Called with the provider name when its tool set actually changed (the UI refreshes).
        self._on_change: list[Callable[[str], None]] = []
        self.set_providers(list(providers or []))  # one place installs the reconnect hooks

    def on_change(self, hook: Callable[[str], None]) -> None:
        self._on_change.append(hook)

    def add(self, provider: ToolProvider) -> None:
        self.set_providers([*self._providers, provider])

    def set_providers(self, providers: list[ToolProvider]) -> None:
        self._providers = list(providers)
        for provider in self._providers:
            # A provider that can reconnect (an MCP server) re-lists itself when it does, so a
            # restarted host does not stay invisible until someone reloads by hand.
            if hasattr(provider, "on_connect"):
                provider.on_connect = self.reindex  # type: ignore[attr-defined]

    async def reindex(self, provider: ToolProvider) -> None:
        """Replace one provider's tools in the index. Safe to call at any time."""
        if provider not in self._providers:
            return
        try:
            specs = await provider.list_tools()
        except Exception as exc:
            self._errors[provider.name] = getattr(provider, "error", None) or f"{type(exc).__name__}: {exc}"
            log.warning("provider %s failed to re-list tools: %s", provider.name, self._errors[provider.name])
            return
        index = {name: entry for name, entry in self._index.items() if entry[0] is not provider}
        for spec in specs:
            index[spec.name] = (provider, spec)
        before = sorted(n for n, (p, _) in self._index.items() if p is provider)
        self._index = index
        after = sorted(s.name for s in specs)
        self._errors.pop(provider.name, None)
        if before != after:
            log.info("provider %s re-listed: %d tools (was %d)", provider.name, len(after), len(before))
            for hook in self._on_change:
                hook(provider.name)

    def forget(self, provider: ToolProvider) -> None:
        """Drop a provider's tools synchronously (used when a connection goes away)."""
        self._index = {name: entry for name, entry in self._index.items() if entry[0] is not provider}

    async def refresh(self) -> None:
        index: dict[str, tuple[ToolProvider, ToolSpec]] = {}
        self._errors = {}
        for provider in self._providers:
            try:
                for spec in await provider.list_tools():
                    if spec.name in index:
                        log.warning("tool %s from %s shadows an earlier provider", spec.name, provider.name)
                    index[spec.name] = (provider, spec)
            except Exception as exc:
                self._errors[provider.name] = getattr(provider, "error", None) or f"{type(exc).__name__}: {exc}"
                log.warning("provider %s failed to list tools: %s", provider.name, self._errors[provider.name])
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
        return [spec for _, spec in self._index.values()]

    def get(self, name: str) -> ToolSpec | None:
        entry = self._index.get(name)
        return entry[1] if entry else None

    def validate(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Return an error string if ``arguments`` do not fit the tool's schema."""
        spec = self.get(name)
        if spec is None:
            return f"unknown tool {name!r}"
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
            return ToolResult.failure(f"unknown tool {name!r}")
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
