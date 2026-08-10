"""Command-line entry point for the local bridge."""

from __future__ import annotations

import logging

from aiohttp import web

from .config import BridgeConfig
from .service import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aioice").setLevel(logging.WARNING)
    config = BridgeConfig.from_env()
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        print=None,
        shutdown_timeout=5,
    )


if __name__ == "__main__":
    main()
