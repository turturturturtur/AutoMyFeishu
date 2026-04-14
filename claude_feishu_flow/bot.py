"""Bot facade - implemented in Step 10."""

from __future__ import annotations

from claude_feishu_flow.config import Config


class Bot:
    """Top-level facade. Users only need: Bot(config).run()"""

    def __init__(self, config: Config) -> None:
        self._config = config

    def get_app(self):  # type: ignore[return]
        from claude_feishu_flow.server.app import create_app
        return create_app(self._config)

    def run(self) -> None:
        import uvicorn
        uvicorn.run(self.get_app(), host=self._config.host, port=self._config.port)
