"""Role → adapter. Adapters are cached per spec so semaphores and HTTP pools are shared.

A run is routed to one LANE (``Settings.run_routing``: chat or background) and every model call
it makes goes to that lane's endpoint — the main loop as the lane's own spec, the planner /
classifier / judge as their own behaviour on the lane's endpoint. The run kind travels in a
context variable set by the engine for the run's task, so features deep inside the run need no
plumbing to be routed with it; a call made outside any run uses the role as configured.
"""

from __future__ import annotations

from contextvars import ContextVar

from jarvis_core.models.base import ModelAdapter
from jarvis_core.models.fake import FakeAdapter
from jarvis_core.models.ollama import OllamaAdapter
from jarvis_core.models.openai_compat import OpenAICompatAdapter
from jarvis_proto import LANES, ModelSpec, Provider, RoleName, RunKind, Settings, ThinkLevel

current_run_kind: ContextVar[RunKind | None] = ContextVar("current_run_kind", default=None)

# The fields that say WHERE a call goes; the rest of a ModelSpec says HOW it behaves.
_ENDPOINT_FIELDS = ("provider", "base_url", "model", "num_ctx", "keep_alive", "timeout_s")


class AdapterFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[str, ModelAdapter] = {}
        self._windows: dict[str, int | None] = {}  # endpoint_key → probed context window
        self.fakes: dict[RoleName, FakeAdapter] = {}  # tests inject per-role scripted adapters

    def update_settings(self, settings: Settings) -> None:
        self._settings = settings
        self._cache.clear()
        self._windows.clear()

    async def context_window(self, kind: RunKind | None = None) -> int | None:
        """The context window, in tokens, of the lane a run of ``kind`` executes on (the current
        run's when omitted; the chat lane outside any run). ``num_ctx`` on the lane's spec is
        the explicit answer; otherwise the endpoint is asked once (vLLM says max_model_len)
        and the answer kept until settings change. None when nobody knows."""
        lane = self.lane_for(kind) or RoleName.CHAT
        spec = self._settings.roles[lane]
        if spec.num_ctx:
            return spec.num_ctx
        key = spec.endpoint_key
        if key not in self._windows:
            try:
                probe = await self.for_role(lane).probe()
                self._windows[key] = probe.context_window
            except Exception:
                self._windows[key] = None
        return self._windows[key]

    def lane_for(self, kind: RunKind | None = None) -> RoleName | None:
        """The lane a run of ``kind`` executes on; the current run's when ``kind`` is omitted;
        None outside any run."""
        kind = kind if kind is not None else current_run_kind.get()
        return self._settings.lane_for(kind) if kind is not None else None

    def spec_for(self, role: RoleName, *, kind: RunKind | None = None) -> ModelSpec:
        """A role's spec as it applies right now: inside a run, a lane role IS the run's lane and
        any other role keeps its behaviour but takes the lane's endpoint."""
        lane = self.lane_for(kind)
        if lane is None:
            return self._settings.roles[role]
        if role in LANES:
            return self._settings.roles[lane]
        where = self._settings.roles[lane].model_dump(include=set(_ENDPOINT_FIELDS))
        return self._settings.roles[role].model_copy(update=where)

    def for_role(
        self,
        role: RoleName,
        *,
        think: bool | None = None,
        think_level: ThinkLevel | None = None,
        kind: RunKind | None = None,
        exact: bool = False,
    ) -> ModelAdapter:
        """The adapter for a role, optionally with a per-run thinking override, routed to the
        lane of ``kind`` (or of the run in progress). ``exact`` takes a lane role as named — the
        failover's way of reaching the OTHER lane from inside a run.

        Overrides produce a sibling adapter (cached by spec) that shares the endpoint's
        semaphore, so concurrency limits still hold per GPU box.
        """
        if exact:
            spec = self._settings.roles[role].with_thinking(think, think_level)
            if role in self.fakes:
                self.fakes[role].spec = spec
                return self.fakes[role]
            key = spec.model_dump_json()
            if key not in self._cache:
                self._cache[key] = self._build(spec)
            return self._cache[key]
        if role in LANES and (lane := self.lane_for(kind)) is not None:
            role = lane
        if role in self.fakes:
            # Rebased on the ROLE's spec, not on the fake's current one. Deriving it from the
            # fake meant an override stuck: once anything asked for think=False, a later call
            # passing None re-derived from the already-off spec and thinking stayed off for the
            # rest of the process — so a test could not see per-step thinking at all.
            fake = self.fakes[role]
            fake.spec = self.spec_for(role, kind=kind).with_thinking(think, think_level)
            return fake
        spec = self.spec_for(role, kind=kind).with_thinking(think, think_level)
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
