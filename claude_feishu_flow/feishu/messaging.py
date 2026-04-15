"""Feishu messaging: send text and card messages."""

from __future__ import annotations

import json
import logging
from pathlib import Path
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
        reply_message_id: str | None = None,
    ) -> str:
        """Send a plain-text message. Returns the created message_id.

        If reply_message_id is provided, uses the Reply API to quote that message.
        """
        content = json.dumps({"text": text})
        if reply_message_id:
            path = f"/im/v1/messages/{reply_message_id}/reply"
            payload = {"msg_type": "text", "content": content}
            data = await self._client.post(path, payload)
        else:
            payload = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": content,
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
        reply_message_id: str | None = None,
    ) -> str:
        """Send an interactive card message. Returns the created message_id.

        If reply_message_id is provided, uses the Reply API to quote that message.
        """
        content = json.dumps(card)
        if reply_message_id:
            path = f"/im/v1/messages/{reply_message_id}/reply"
            payload = {"msg_type": "interactive", "content": content}
            data = await self._client.post(path, payload)
        else:
            payload = {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": content,
            }
            data = await self._client.post(
                _SEND_MSG_PATH,
                payload,
                params={"receive_id_type": receive_id_type},
            )
        message_id: str = data.get("data", {}).get("message_id", "")
        logger.info("Sent card message to %s; message_id=%s", receive_id, message_id)
        return message_id

    async def send_markdown(
        self,
        receive_id: str,
        markdown_text: str,
        receive_id_type: ReceiveIdType = "chat_id",
        reply_message_id: str | None = None,
    ) -> str:
        """Send a headerless interactive card that renders markdown_text as Markdown."""
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": markdown_text}],
        }
        return await self.send_card(receive_id, card, receive_id_type=receive_id_type, reply_message_id=reply_message_id)

    async def delete_message(self, message_id: str) -> None:
        """撤回一条消息。失败只记录 warning，不抛异常，避免中断主流程。"""
        try:
            await self._client.delete(f"/im/v1/messages/{message_id}")
            logger.info("撤回消息 message_id=%s 成功", message_id)
        except Exception as exc:
            logger.warning("撤回消息 %s 失败: %s", message_id, exc)

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
        reply_message_id: str | None = None,
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
        element_action = {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "💬 进入该实验专属会话"},
                    "type": "primary",
                    "value": {"key": "enter_session", "task_id": task_id},
                }
            ],
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
                {"tag": "hr"},
                element_action,
            ],
        }
        return await self.send_card(receive_id, card, receive_id_type=receive_id_type, reply_message_id=reply_message_id)  # type: ignore[arg-type]

    async def send_help_card(
        self,
        receive_id: str,
        receive_id_type: str,
        error_msg: str = "",
        reply_message_id: str | None = None,
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

                "**🤖 自然语言对话（推荐）**\n"
                "直接用自然语言描述你的需求，大管家会自动理解并触发相应操作。\n"
                "示例：`帮我训练一个 MNIST 分类器`\n"
                "示例：`有哪些实验？`\n"
                "示例：`把 exp_xxxxx 的学习率改为 1e-4`\n"
                "示例：`显卡状态如何？`\n\n"

                "**🚀 新建实验（快捷指令）**\n"
                "```\n/launch <实验描述>\n```\n"
                "示例：`/launch 帮我训练一个 MNIST 分类器`\n"
                "系统会自动生成代码、执行、并汇报结果。\n\n"

                "**🔁 新建实验 + 自动修复**\n"
                "```\n/launch <实验描述> --retry <次数>\n```\n"
                "示例：`/launch 帮我训练一个 MNIST 分类器 --retry 3`\n"
                "脚本运行失败时，AI 自动分析错误并最多重试 N 次。\n"
                "<font color='grey'>不加 --retry 则失败后直接汇报，不自动修复。</font>\n\n"

                "**📋 列出所有实验**\n"
                "```\n/list\n```\n\n"

                "**✏️ 修改已有实验（交互式多轮对话）**\n"
                "```\n/edit exp_<uuid> <修改指令>\n```\n"
                "示例：`/edit exp_19caeba9 把学习率改为 1e-4`\n"
                "支持多轮对话，修改完毕后自动重新运行。\n\n"

                "**✏️ 修改实验 + 自动修复**\n"
                "```\n/edit exp_<uuid> <修改指令> --retry 2\n```\n\n"

                "**🕵️ 代码审阅（独立审阅，不执行）**\n"
                "```\n/review exp_<uuid>\n```\n"
                "对已生成的实验代码进行静态审阅，检查逻辑漏洞、OOM 风险、语法错误等，输出审阅报告。\n"
                "<font color='grey'>不会启动实验执行。也可自然语言触发：「帮我审阅 exp_xxxxx」</font>\n\n"

                "**❌ 取消编辑会话**\n"
                "```\n/cancel\n```\n"
                "取消当前正在进行的 /edit 多轮对话。\n\n"

                "**🔬 进入实验专属对话（Sub Agent）**\n"
                "点击实验卡片或 /list 列表中的 **进入会话** 按钮，\n"
                "即可与该实验的专属 AI 助手对话。\n"
                "Sub Agent 可读取日志、修改代码、重启实验。\n\n"

                "**🚪 退出 Sub Agent 会话**\n"
                "```\n/exit\n```\n"
                "退出 Sub Agent 模式，返回主界面。\n\n"

                "**🆘 帮助**\n"
                "```\n/help\n```\n"
                "显示此帮助卡片。"
            ),
        })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📖 使用帮助 / Help"},
                "template": "yellow",
            },
            "elements": elements,
        }
        return await self.send_card(receive_id, card, receive_id_type=receive_id_type, reply_message_id=reply_message_id)  # type: ignore[arg-type]

    async def send_list_card(
        self,
        receive_id: str,
        receive_id_type: str,
        entries: list[Path],
        reply_message_id: str | None = None,
    ) -> str:
        """Send a card listing all existing experiments.

        Args:
            receive_id:      Feishu chat_id or open_id.
            receive_id_type: "chat_id" | "open_id" etc.
            entries:         Experiment directories sorted newest-first.

        Returns:
            Created message_id.
        """
        if not entries:
            body_elements = [{"tag": "markdown", "content": "_(暂无实验记录)_"}]
        else:
            body_elements = []
            for d in entries:
                status_icon = "✅" if (d / "results" / "summary.md").exists() else "⏳"
                body_elements.append({
                    "tag": "markdown",
                    "content": f"{status_icon} `{d.name}`",
                })
                body_elements.append({
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "进入会话"},
                            "type": "primary",
                            "value": {"key": "enter_session", "task_id": d.name},
                        }
                    ],
                })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📋 实验列表（共 {len(entries)} 个）",
                },
                "template": "blue",
            },
            "elements": [
                *body_elements,
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        "✅ 已完成　⏳ 未完成/运行中\n"
                        "点击 **进入会话** 按钮与实验 Sub Agent 对话\n"
                        "在会话中发送 `/exit` 返回主界面"
                    ),
                },
            ],
        }
        return await self.send_card(receive_id, card, receive_id_type=receive_id_type, reply_message_id=reply_message_id)  # type: ignore[arg-type]
