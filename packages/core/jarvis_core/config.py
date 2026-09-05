"""Process configuration — environment only. Everything else lives in the settings table."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(slots=True)
class CoreConfig:
    home: Path = field(default_factory=lambda: Path(_env("JARVIS_HOME", "./data")).resolve())
    host: str = field(default_factory=lambda: _env("JARVIS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("JARVIS_PORT", "9020")))
    token: str | None = field(default_factory=lambda: os.environ.get("JARVIS_TOKEN") or None)
    web_dist: Path | None = field(
        default_factory=lambda: Path(p).resolve() if (p := os.environ.get("JARVIS_WEB_DIST")) else None
    )
    log_level: str = field(default_factory=lambda: _env("JARVIS_LOG_LEVEL", "INFO"))
    max_concurrent_runs: int = field(default_factory=lambda: int(_env("JARVIS_MAX_RUNS", "3")))

    @property
    def db_path(self) -> Path:
        return self.home / "jarvis2.db"

    def resolve_web_dist(self) -> Path | None:
        if self.web_dist is not None:
            return self.web_dist if self.web_dist.exists() else None
        # Repo layout: packages/core/jarvis_core/config.py → ../../../web/dist
        candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
        return candidate if candidate.exists() else None
