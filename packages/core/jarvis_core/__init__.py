"""Jarvis V2 core — run engine, agent loop, model adapters, web server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jarvis-core")  # one source of truth: pyproject.toml
except PackageNotFoundError:  # pragma: no cover - running from a source tree without an install
    __version__ = "0.0.0+source"
