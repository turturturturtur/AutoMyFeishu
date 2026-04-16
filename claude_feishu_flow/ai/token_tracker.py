"""Global token usage tracker shared by all AI clients."""
import asyncio
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE = Path("logs/token_usage.json")
_DEFAULT: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}


class TokenTracker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        os.makedirs("logs", exist_ok=True)
        if _FILE.exists():
            try:
                self._data: dict[str, int] = json.loads(_FILE.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Failed to load token_usage.json, starting fresh.")
                self._data = dict(_DEFAULT)
        else:
            self._data = dict(_DEFAULT)
            _FILE.write_text(json.dumps(self._data), encoding="utf-8")

    async def record(self, input_tokens: int, output_tokens: int) -> None:
        async with self._lock:
            self._data["input_tokens"] += input_tokens
            self._data["output_tokens"] += output_tokens
            try:
                _FILE.write_text(json.dumps(self._data), encoding="utf-8")
            except Exception:
                logger.warning("Failed to persist token_usage.json.")

    def get(self) -> dict[str, int]:
        return dict(self._data)


_tracker: TokenTracker | None = None


def get_tracker() -> TokenTracker:
    global _tracker
    if _tracker is None:
        _tracker = TokenTracker()
    return _tracker
