"""Feishu tenant_access_token manager with background auto-refresh."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
# Refresh this many seconds before the token actually expires
_REFRESH_BUFFER_SECONDS = 300


class TokenManager:
    """Maintains a cached tenant_access_token and refreshes it proactively.

    Usage (inside FastAPI lifespan):
        manager = TokenManager(http_client, app_id, app_secret)
        await manager.start()          # begins background refresh loop
        token = await manager.get_token()
        await manager.stop()           # graceful shutdown
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        app_id: str,
        app_secret: str,
    ) -> None:
        self._http = http
        self._app_id = app_id
        self._app_secret = app_secret

        self._token: Optional[str] = None
        self._expires_at: float = 0.0  # epoch seconds
        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    async def start(self) -> None:
        """Fetch initial token and start background refresh loop."""
        await self._fetch_and_cache()  # acquires lock internally
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="feishu-token-refresh"
        )
        logger.info("TokenManager started; token expires at %.0f", self._expires_at)

    async def stop(self) -> None:
        """Cancel background refresh task gracefully."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("TokenManager stopped")

    async def get_token(self) -> str:
        """Return a valid cached token.

        Fast path (normal operation): token is cached and fresh — no lock needed.
        Slow path (first call or unexpected expiry): acquire lock and re-fetch.
        The lock prevents concurrent callers from making duplicate refresh requests.
        """
        # Fast path: no locking required when token is valid
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token

        # Slow path: acquire lock, re-check, then fetch if still needed
        async with self._lock:
            # Double-check: another coroutine may have refreshed while we waited
            if self._token is None or time.monotonic() >= self._expires_at:
                logger.warning("Token missing or expired; fetching synchronously")
                await self._fetch_and_cache_unlocked()
            assert self._token is not None
            return self._token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_and_cache(self) -> None:
        """Acquire lock then fetch. Used by the background refresh loop."""
        async with self._lock:
            await self._fetch_and_cache_unlocked()

    async def _fetch_and_cache_unlocked(self) -> None:
        """Fetch a fresh token and update state. Caller must hold self._lock."""
        payload = {
            "app_id": self._app_id,
            "app_secret": self._app_secret,
        }
        resp = await self._http.post(_TOKEN_URL, json=payload)
        resp.raise_for_status()
        data: dict = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"Feishu token fetch failed: code={data.get('code')} msg={data.get('msg')}"
            )

        self._token = data["tenant_access_token"]
        expires_in: int = data.get("expire", 7200)
        self._expires_at = time.monotonic() + expires_in - _REFRESH_BUFFER_SECONDS

        logger.info(
            "Token refreshed; valid for ~%ds (buffer %ds)",
            expires_in,
            _REFRESH_BUFFER_SECONDS,
        )

    async def _refresh_loop(self) -> None:
        """Background loop: sleep until near-expiry then refresh."""
        while True:
            sleep_for = max(0.0, self._expires_at - time.monotonic())
            logger.debug("Next token refresh in %.0f seconds", sleep_for)
            await asyncio.sleep(sleep_for)
            try:
                await self._fetch_and_cache()
            except Exception as exc:
                # Log but do not crash; next iteration will retry quickly
                logger.exception("Token refresh failed: %s — retrying in 30s", exc)
                await asyncio.sleep(30)
