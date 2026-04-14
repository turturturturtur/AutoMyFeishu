"""Thin async HTTP wrapper for the Feishu Open API."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from claude_feishu_flow.feishu.auth import TokenManager

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """Async HTTP client for Feishu Open API.

    All methods automatically inject the Authorization header via TokenManager.
    """

    def __init__(
        self,
        token_manager: TokenManager,
        http: httpx.AsyncClient,
        base_url: str = FEISHU_BASE_URL,
    ) -> None:
        self._token_manager = token_manager
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def _headers(self) -> dict[str, str]:
        token = await self._token_manager.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """POST to a Feishu API path, returning the parsed JSON body."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        headers = await self._headers()
        logger.debug("POST %s payload=%s", url, payload)
        resp = await self._http.post(url, json=payload, headers=headers, params=params)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        if data.get("code") not in (0, None):
            logger.warning("Feishu API non-zero code: %s", data)
        return data

    async def get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """GET from a Feishu API path, returning the parsed JSON body."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        headers = await self._headers()
        logger.debug("GET %s params=%s", url, params)
        resp = await self._http.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        if data.get("code") not in (0, None):
            logger.warning("Feishu API non-zero code: %s", data)
        return data
