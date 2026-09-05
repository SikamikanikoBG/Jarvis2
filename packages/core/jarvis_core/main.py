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
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
