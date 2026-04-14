"""Unit tests for feishu.auth (TokenManager) and feishu.client (FeishuClient)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_token_response(token: str = "t-test123", expire: int = 7200) -> dict:
    return {"code": 0, "tenant_access_token": token, "expire": expire, "msg": "ok"}


def _mock_http(token_resp: dict) -> httpx.AsyncClient:
    """Return a mock AsyncClient whose post() returns the given token response."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.json.return_value = token_resp
    mock_resp.raise_for_status = MagicMock()

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=mock_resp)
    http.get = AsyncMock(return_value=mock_resp)
    return http


# ---------------------------------------------------------------------------
# TokenManager tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_manager_start_and_get():
    """start() fetches token; get_token() returns it."""
    from claude_feishu_flow.feishu.auth import TokenManager

    http = _mock_http(_make_token_response("t-abc"))
    mgr = TokenManager(http, "app_id", "app_secret")

    await mgr.start()
    token = await mgr.get_token()
    assert token == "t-abc"

    await mgr.stop()
    # Verify exactly one POST was made during start()
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_token_manager_raises_on_api_error():
    """start() raises RuntimeError when Feishu returns non-zero code."""
    from claude_feishu_flow.feishu.auth import TokenManager

    error_resp = MagicMock(spec=httpx.Response)
    error_resp.json.return_value = {"code": 10012, "msg": "app not found"}
    error_resp.raise_for_status = MagicMock()

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=error_resp)

    mgr = TokenManager(http, "bad_id", "bad_secret")
    with pytest.raises(RuntimeError, match="Feishu token fetch failed"):
        await mgr.start()


@pytest.mark.asyncio
async def test_token_manager_fallback_refresh_on_expiry():
    """get_token() re-fetches when token is found expired at call time."""
    from claude_feishu_flow.feishu.auth import TokenManager

    http = _mock_http(_make_token_response("t-fresh"))
    mgr = TokenManager(http, "app_id", "app_secret")

    # Manually set an already-expired state
    mgr._token = "t-stale"
    mgr._expires_at = time.monotonic() - 1.0  # already past

    token = await mgr.get_token()
    assert token == "t-fresh"
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_token_manager_stop_cancels_task():
    """stop() cancels the background refresh task without raising."""
    from claude_feishu_flow.feishu.auth import TokenManager

    http = _mock_http(_make_token_response())
    mgr = TokenManager(http, "app_id", "app_secret")
    await mgr.start()
    assert mgr._refresh_task is not None
    assert not mgr._refresh_task.done()

    await mgr.stop()
    assert mgr._refresh_task.done()


# ---------------------------------------------------------------------------
# FeishuClient tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feishu_client_post_injects_auth_header():
    """post() includes Authorization: Bearer <token> header."""
    from claude_feishu_flow.feishu.auth import TokenManager
    from claude_feishu_flow.feishu.client import FeishuClient

    # TokenManager stub that returns a fixed token
    token_mgr = AsyncMock(spec=TokenManager)
    token_mgr.get_token = AsyncMock(return_value="t-mytoken")

    api_resp = MagicMock(spec=httpx.Response)
    api_resp.json.return_value = {"code": 0, "data": {}}
    api_resp.raise_for_status = MagicMock()

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=api_resp)

    client = FeishuClient(token_mgr, http)
    result = await client.post("/im/v1/messages", {"msg": "hi"})

    assert result == {"code": 0, "data": {}}
    call_kwargs = http.post.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer t-mytoken"


@pytest.mark.asyncio
async def test_feishu_client_get_injects_auth_header():
    """get() includes Authorization header."""
    from claude_feishu_flow.feishu.auth import TokenManager
    from claude_feishu_flow.feishu.client import FeishuClient

    token_mgr = AsyncMock(spec=TokenManager)
    token_mgr.get_token = AsyncMock(return_value="t-gettoken")

    api_resp = MagicMock(spec=httpx.Response)
    api_resp.json.return_value = {"code": 0, "items": []}
    api_resp.raise_for_status = MagicMock()

    http = AsyncMock(spec=httpx.AsyncClient)
    http.get = AsyncMock(return_value=api_resp)

    client = FeishuClient(token_mgr, http)
    result = await client.get("/bitable/v1/apps/xxx/tables/yyy/records")

    assert result["code"] == 0
    call_kwargs = http.get.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer t-gettoken"


@pytest.mark.asyncio
async def test_feishu_client_raises_on_http_error():
    """post() propagates httpx.HTTPStatusError when raise_for_status() raises."""
    from claude_feishu_flow.feishu.auth import TokenManager
    from claude_feishu_flow.feishu.client import FeishuClient

    token_mgr = AsyncMock(spec=TokenManager)
    token_mgr.get_token = AsyncMock(return_value="t-token")

    bad_resp = MagicMock(spec=httpx.Response)
    bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )

    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=bad_resp)

    client = FeishuClient(token_mgr, http)
    with pytest.raises(httpx.HTTPStatusError):
        await client.post("/some/path", {})
