"""Bot facade — the only class users need to instantiate."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from claude_feishu_flow.config import Config

logger = logging.getLogger(__name__)


class Bot:
    """Top-level facade for claude-feishu-flow.

    Typical usage::

        from claude_feishu_flow import Bot, Config

        bot = Bot(Config())
        bot.run()           # blocking: starts uvicorn on config.host:config.port

    For ASGI deployment (gunicorn, etc.)::

        app = Bot(Config()).get_app()
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._app: FastAPI | None = None

    def get_app(self) -> FastAPI:
        """Return the FastAPI application (created once, cached)."""
        if self._app is None:
            from claude_feishu_flow.server.app import create_app
            self._app = create_app(self._config)
        return self._app

    def run(self) -> None:
        """Start the uvicorn server (blocking)."""
        import uvicorn
        logger.info(
            "Starting Bot on %s:%d", self._config.host, self._config.port
        )
        uvicorn.run(
            self.get_app(),
            host=self._config.host,
            port=self._config.port,
        )
