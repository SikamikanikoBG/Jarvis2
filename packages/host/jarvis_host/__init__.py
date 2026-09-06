"""Jarvis V2 host daemon — a machine's local capabilities as an MCP server (Phase 3)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # One source of truth: pyproject.toml. A second literal here drifted for five releases and
    # the host reported 2.0.0a1 while it was running 2.0.0a6.
    __version__ = version("jarvis-host")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without an install
    __version__ = "0.0.0+source"
