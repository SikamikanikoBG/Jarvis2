"""``host.toml`` — the per-machine configuration of the host daemon.

Lives at ``%LOCALAPPDATA%\\Jarvis2\\host.toml`` (override with ``JARVIS_HOST_CONFIG``). Created on
first run with a freshly generated bearer token, which is printed exactly once. Nothing here is
ever synced to the core: the whole point of the host is that machine-local settings stay local.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LISTEN = "0.0.0.0:9030"
ENV_CONFIG = "JARVIS_HOST_CONFIG"


class ConfigError(ValueError):
    """The config file exists but cannot be used."""


@dataclass(frozen=True)
class HostConfig:
    name: str
    listen: str = DEFAULT_LISTEN
    token: str = ""
    outlook_accounts: tuple[str, ...] = ()
    fs_roots: tuple[Path, ...] = ()
    shell_allow: bool = True
    screen_enabled: bool = True
    path: Path | None = field(default=None, compare=False)

    @property
    def host(self) -> str:
        host, _, _ = self.listen.rpartition(":")
        return host or "0.0.0.0"

    @property
    def port(self) -> int:
        _, _, port = self.listen.rpartition(":")
        try:
            return int(port)
        except ValueError as exc:
            raise ConfigError(f"listen must be host:port, got {self.listen!r}") from exc


def default_config_path() -> Path:
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Jarvis2" / "host.toml"
    return Path.home() / ".jarvis2" / "host.toml"


def default_name() -> str:
    return (socket.gethostname().split(".")[0] or "host").lower()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def render_toml(cfg: HostConfig) -> str:
    """Serialise a config. JSON string escaping is a subset of TOML basic-string escaping."""
    q = json.dumps
    roots = ", ".join(q(str(p)) for p in cfg.fs_roots)
    accounts = ", ".join(q(a) for a in cfg.outlook_accounts)
    return (
        f"# jarvis-host configuration — created {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        f"# The token is the bearer secret the core sends; keep this file private.\n"
        f"name = {q(cfg.name)}\n"
        f"listen = {q(cfg.listen)}\n"
        f"token = {q(cfg.token)}\n"
        f"\n[outlook]\n"
        f"# Allow-list of Outlook store display names exposed to the core; empty = every store.\n"
        f"accounts = [{accounts}]\n"
        f"\n[fs]\n"
        f"# Directories fs_* tools may touch; anything outside is refused.\n"
        f"roots = [{roots}]\n"
        f"\n[shell]\n"
        f"allow = {'true' if cfg.shell_allow else 'false'}\n"
        f"\n[screen]\n"
        f"enabled = {'true' if cfg.screen_enabled else 'false'}\n"
    )


def _as_str_list(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
        raise ConfigError(f"{key} must be a list of strings")
    return tuple(str(v) for v in value)  # pyright: ignore[reportUnknownVariableType]


def parse_config(text: str, path: Path | None = None) -> HostConfig:
    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path or 'host.toml'}: {exc}") from exc
    outlook = raw.get("outlook") or {}
    fs = raw.get("fs") or {}
    shell = raw.get("shell") or {}
    screen = raw.get("screen") or {}
    roots = _as_str_list(fs.get("roots"), "fs.roots") or (str(Path.home()),)
    cfg = HostConfig(
        name=str(raw.get("name") or default_name()),
        listen=str(raw.get("listen") or DEFAULT_LISTEN),
        token=str(raw.get("token") or ""),
        outlook_accounts=_as_str_list(outlook.get("accounts"), "outlook.accounts"),
        fs_roots=tuple(Path(r).expanduser() for r in roots),
        shell_allow=bool(shell.get("allow", True)),
        screen_enabled=bool(screen.get("enabled", True)),
        path=path,
    )
    cfg.port  # noqa: B018 — validates listen early
    return cfg


def load_config(path: Path | None = None) -> tuple[HostConfig, bool]:
    """Load the config, creating it (with a new token) when missing.

    Returns ``(config, created)``. A config that exists without a token gets one written back,
    reported as ``created`` so the caller prints it once.
    """
    path = path or default_config_path()
    created = False
    if path.exists():
        cfg = parse_config(path.read_text(encoding="utf-8"), path)
    else:
        cfg = HostConfig(name=default_name(), fs_roots=(Path.home(),), path=path)
        created = True
    if not cfg.token:
        cfg = replace(cfg, token=generate_token())
        created = True
    if created:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_toml(cfg), encoding="utf-8")
    return cfg, created
