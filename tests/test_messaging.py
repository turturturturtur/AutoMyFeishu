"""Unit tests for feishu.messaging (Messaging)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_feishu_flow.feishu.client import FeishuClient
from claude_feishu_flow.feishu.messaging import Messaging


def _mock_client(response_data: dict) -> FeishuClient:
    client = AsyncMock(spec=FeishuClient)
    client.post = AsyncMock(return_value=response_data)
    return client


# ---------------------------------------------------------------------------
# send_text tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_text_posts_correct_payload():
    """send_text() calls POST with msg_type=text and JSON-encoded content."""
    client = _mock_client({"code": 0, "data": {"message_id": "om_abc"}})
    messaging = Messaging(client)

    msg_id = await messaging.send_text("oc_chat001", "Hello!")

    assert msg_id == "om_abc"
    client.post.assert_called_once()
    call_args = client.post.call_args

    path = call_args.args[0]
    payload = call_args.args[1]
    params = call_args.kwargs.get("params", {})

    assert path == "/im/v1/messages"
    assert payload["receive_id"] == "oc_chat001"
    assert payload["msg_type"] == "text"
    assert json.loads(payload["content"])["text"] == "Hello!"
    assert params["receive_id_type"] == "chat_id"


@pytest.mark.asyncio
async def test_send_text_uses_open_id_type():
    client = _mock_client({"code": 0, "data": {"message_id": "om_xyz"}})
    messaging = Messaging(client)

    await messaging.send_text("ou_user001", "Hi", receive_id_type="open_id")

    params = client.post.call_args.kwargs.get("params", {})
    assert params["receive_id_type"] == "open_id"


@pytest.mark.asyncio
async def test_send_text_returns_empty_string_when_no_message_id():
    """Gracefully handles response missing data.message_id."""
    client = _mock_client({"code": 0, "data": {}})
    messaging = Messaging(client)

    msg_id = await messaging.send_text("oc_chat001", "test")
    assert msg_id == ""


# ---------------------------------------------------------------------------
# send_card tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_card_posts_interactive_type():
    """send_card() sends msg_type=interactive with JSON card content."""
    client = _mock_client({"code": 0, "data": {"message_id": "om_card01"}})
    messaging = Messaging(client)

    card = {"config": {"wide_screen_mode": True}, "elements": []}
    msg_id = await messaging.send_card("oc_chat001", card)

    assert msg_id == "om_card01"
    payload = client.post.call_args.args[1]
    assert payload["msg_type"] == "interactive"
    assert json.loads(payload["content"]) == card
