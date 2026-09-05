"""``jarvis-core`` entry point."""

from __future__ import annotations

import logging

import uvicorn
from dotenv import load_dotenv

from jarvis_core.app import create_app
from jarvis_core.config import CoreConfig


def main() -> None:
    load_dotenv()
    config = CoreConfig()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        ws_max_size=8 * 1024 * 1024,  # browser screenshots ride the WS as base64
        # Behind Tailscale Serve the proxy is the Docker gateway, not 127.0.0.1, so the default
        # allow-list would drop X-Forwarded-Proto and every generated URL would say http://.
        # Safe here: the port is only reachable through the host's own mapping.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
