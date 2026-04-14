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

    async def send_experiment_card(
        self,
        receive_id: str,
        receive_id_type: str,
        task_id: str,
        command: str,
        plan_summary: str,
        result_summary: str,
        status: str,
        duration: float,
        repair_count: int = 0,
    ) -> str:
        """Send a structured experiment report card.

        Args:
            receive_id:      Feishu chat_id or open_id.
            receive_id_type: "chat_id" | "open_id" etc.
            task_id:         Experiment UUID string.
            command:         Original user instruction.
            plan_summary:    First 500 chars of plan.md.
            result_summary:  Claude-generated Markdown analysis.
            status:          "success" | "failed".
            duration:        Wall-clock execution time in seconds.
            repair_count:    Number of self-healing repair attempts made.

        Returns:
            Created message_id.
        """
        header_template = "blue" if status == "success" else "red"
        status_emoji = "✅" if status == "success" else "❌"

        repair_info = f"\n\n**🔧 自动修复次数:** {repair_count}" if repair_count > 0 else ""

        element_info = {
            "tag": "markdown",
            "content": (
                f"**🎯 用户指令**\n{command}\n\n"
                f"**⏱️ 耗时:** {duration:.1f}s　|　**状态:** {status_emoji} {status}"
                f"{repair_info}\n\n"
                f"**📝 实验计划**\n{plan_summary}"
            ),
        }
        element_analysis = {
            "tag": "markdown",
            "content": f"**📊 实验结果分析**\n\n{result_summary}",
        }

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🧪 实验报告: {task_id}"},
                "template": header_template,
            },
            "elements": [
                element_info,
                {"tag": "hr"},
                element_analysis,
            ],
        }
        return await self.send_card(receive_id, card, receive_id_type=receive_id_type)  # type: ignore[arg-type]

    async def send_help_card(
        self,
        receive_id: str,
        receive_id_type: str,
        error_msg: str = "",
    ) -> str:
        """Send a help card showing available commands and usage.

        Args:
            receive_id:      Feishu chat_id or open_id.
            receive_id_type: "chat_id" | "open_id" etc.
            error_msg:       Optional error description to show at the top.

        Returns:
            Created message_id.
        """
        elements = []
        if error_msg:
            elements.append({
                "tag": "markdown",
                "content": f"**⚠️ 解析错误**\n{error_msg}",
            })
            elements.append({"tag": "hr"})

        elements.append({
            "tag": "markdown",
            "content": (
                "**📖 使用帮助**\n\n"
                "**新建实验**\n"
                "```\n<实验描述>\n```\n\n"
                "**修改已有实验**\n"
                "```\n/edit exp_<uuid> <修改指令>\n```\n"
                "示例：`/edit exp_19caeba9-bfac-440a-9314-7cfe0244a165 把输出改成英文`\n\n"
                "**自动修复模式**\n"
                "```\n<指令> --retry <次数>\n```\n"
                "示例：`帮我分析数据 --retry 3`\n\n"
                "**组合使用**\n"
                "```\n/edit exp_<uuid> <修改指令> --retry 2\n```"
            ),
        })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❓ 命令帮助"},
                "template": "yellow",
            },
            "elements": elements,
        }
        return await self.send_card(receive_id, card, receive_id_type=receive_id_type)  # type: ignore[arg-type]
