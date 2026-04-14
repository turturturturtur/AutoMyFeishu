"""Feishu messaging: send text and card messages."""

from __future__ import annotations

import json
import logging
from typing import Literal

from claude_feishu_flow.feishu.client import FeishuClient

logger = logging.getLogger(__name__)

ReceiveIdType = Literal["open_id", "user_id", "union_id", "email", "chat_id"]

_SEND_MSG_PATH = "/im/v1/messages"


class Messaging:
    """Send messages via the Feishu Im API."""

    def __init__(self, client: FeishuClient) -> None:
        self._client = client

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: ReceiveIdType = "chat_id",
    ) -> str:
        """Send a plain-text message. Returns the created message_id."""
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        data = await self._client.post(
            _SEND_MSG_PATH,
            payload,
            params={"receive_id_type": receive_id_type},
        )
        message_id: str = data.get("data", {}).get("message_id", "")
        logger.info("Sent text message to %s; message_id=%s", receive_id, message_id)
        return message_id

    async def send_card(
        self,
        receive_id: str,
        card: dict,
        receive_id_type: ReceiveIdType = "chat_id",
    ) -> str:
        """Send an interactive card message. Returns the created message_id."""
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        data = await self._client.post(
            _SEND_MSG_PATH,
            payload,
            params={"receive_id_type": receive_id_type},
        )
        message_id: str = data.get("data", {}).get("message_id", "")
        logger.info("Sent card message to %s; message_id=%s", receive_id, message_id)
        return message_id
