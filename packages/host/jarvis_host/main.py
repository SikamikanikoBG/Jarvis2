"""``jarvis-host`` entry point."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

import uvicorn

from jarvis_host import __version__
from jarvis_host.config import ConfigError, HostConfig, default_config_path, load_config
from jarvis_host.server import build_app, make_deps

log = logging.getLogger("jarvis_host")


def _announce(cfg: HostConfig, created: bool) -> None:
    hostname = socket.gethostname()
    print(f"jarvis-host {__version__} '{cfg.name}' — config {cfg.path}", file=sys.stderr)
    if created:
        print(
            "\nNew token generated (shown once; it lives in the config file above):\n"
            f"  token: {cfg.token}\n\n"
            "Register in the core's Settings.mcp_servers:\n"
            f'  {{"name": "{cfg.name}", "transport": "streamable_http", "url": "http://{hostname}:{cfg.port}/mcp",\n'
            f'   "headers": {{"Authorization": "Bearer {cfg.token}"}}}}\n',
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis-host", description="Jarvis V2 host daemon (MCP over streamable HTTP)")
    parser.add_argument("--config", type=Path, default=None, help=f"host.toml path (default {default_config_path()})")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--version", action="version", version=f"jarvis-host {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg, created = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"jarvis-host: {exc}") from exc
    _announce(cfg, created)
    log.info(
        "listening on %s (fs roots: %s, shell: %s, screen: %s, outlook allow-list: %s)",
        cfg.listen,
        [str(r) for r in cfg.fs_roots],
        cfg.shell_allow,
        cfg.screen_enabled,
        list(cfg.outlook_accounts) or "all",
    )
    app = build_app(cfg, make_deps(cfg))
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=args.log_level, access_log=False)


if __name__ == "__main__":
    main()
