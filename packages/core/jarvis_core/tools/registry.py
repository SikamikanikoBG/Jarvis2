"""Tool providers and the registry the loop calls into.

Phase 1 exposes tools flat. Derived facades per namespace (DESIGN §6) arrive with the MCP
provider in Phase 3 — the registry is the only place that will change.
"""

from __future__ import annotations

import asyncio
import logging
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
        self._providers: list[ToolProvider] = list(providers or [])
        self._index: dict[str, tuple[ToolProvider, ToolSpec]] = {}

    def add(self, provider: ToolProvider) -> None:
        self._providers.append(provider)

    def set_providers(self, providers: list[ToolProvider]) -> None:
        self._providers = list(providers)

    async def refresh(self) -> None:
        index: dict[str, tuple[ToolProvider, ToolSpec]] = {}
        self._errors: dict[str, str] = {}
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
        errors = getattr(self, "_errors", {})
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
