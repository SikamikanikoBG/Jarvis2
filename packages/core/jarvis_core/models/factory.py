"""Role → adapter. Adapters are cached per spec so semaphores and HTTP pools are shared."""

from __future__ import annotations

from jarvis_core.models.base import ModelAdapter
from jarvis_core.models.fake import FakeAdapter
from jarvis_core.models.ollama import OllamaAdapter
from jarvis_core.models.openai_compat import OpenAICompatAdapter
from jarvis_proto import ModelSpec, Provider, RoleName, Settings, ThinkLevel


class AdapterFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[str, ModelAdapter] = {}
        self.fakes: dict[RoleName, FakeAdapter] = {}  # tests inject per-role scripted adapters

    def update_settings(self, settings: Settings) -> None:
        self._settings = settings
        self._cache.clear()

    def spec_for(self, role: RoleName) -> ModelSpec:
        return self._settings.roles[role]

    def for_role(
        self, role: RoleName, *, think: bool | None = None, think_level: ThinkLevel | None = None
    ) -> ModelAdapter:
        """The adapter for a role, optionally with a per-run thinking override.

        Overrides produce a sibling adapter (cached by spec) that shares the endpoint's
        semaphore, so concurrency limits still hold per GPU box.
        """
        if role in self.fakes:
            fake = self.fakes[role]
            fake.spec = fake.spec.with_thinking(think, think_level)
            return fake
        spec = self.spec_for(role).with_thinking(think, think_level)
        key = spec.model_dump_json()
        adapter = self._cache.get(key)
        if adapter is None:
            adapter = self._build(spec)
            self._cache[key] = adapter
        return adapter

    def _build(self, spec: ModelSpec) -> ModelAdapter:
        limit = self._settings.max_concurrent_runs_per_endpoint
        if spec.provider is Provider.OLLAMA:
            return OllamaAdapter(spec, concurrency=limit)
        if spec.provider is Provider.VLLM:
            return OpenAICompatAdapter(spec, concurrency=limit)
        return FakeAdapter(spec=spec)

    def all_specs(self) -> dict[RoleName, ModelSpec]:
        return dict(self._settings.roles)
