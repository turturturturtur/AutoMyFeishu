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

    async def send_image(
        self,
        receive_id: str,
        image_key: str,
        receive_id_type: ReceiveIdType = "chat_id",
        reply_message_id: str | None = None,
    ) -> str:
        """Send a Feishu image message using an already-uploaded image_key.

        Args:
            receive_id:       Feishu chat_id or open_id.
            image_key:        The image_key returned by FeishuClient.upload_image().
            receive_id_type:  "chat_id" | "open_id" etc.
            reply_message_id: If provided, sends as a reply to that message.

        Returns:
            Created message_id.
        """
        content = json.dumps({"image_key": image_key})
        if reply_message_id:
            path = f"/im/v1/messages/{reply_message_id}/reply"
            payload: dict = {"msg_type": "image", "content": content}
            data = await self._client.post(path, payload)
        else:
            payload = {
                "receive_id": receive_id,
                "msg_type": "image",
                "content": content,
            }
            data = await self._client.post(
                _SEND_MSG_PATH,
                payload,
                params={"receive_id_type": receive_id_type},
            )
        msg_id: str = data["data"]["message_id"]
        logger.info("send_image message_id=%s", msg_id)
        return msg_id

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
        alias: str | None = None,
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
            alias:           Optional human-readable name shown in card title instead of UUID.

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
                "title": {"tag": "plain_text", "content": f"🧪 实验报告: {alias or task_id}"},
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

    async def send_document_card(
        self,
        receive_id: str,
        receive_id_type: str,
        instruction: str,
        document_text: str,
        save_path: str,
        reply_message_id: str | None = None,
    ) -> str:
        """Send a card announcing that a drafted document is ready.

        Shows the writing instruction, character count, server save path, and
        a 500-character preview of the document content.

        Args:
            receive_id:      Feishu chat_id or open_id.
            receive_id_type: "chat_id" | "open_id" etc.
            instruction:     The original writing instruction.
            document_text:   The full generated Markdown document.
            save_path:       Absolute path where the document was saved on server.
            reply_message_id: Optional message_id to reply to.

        Returns:
            Created message_id.
        """
        preview = document_text[:500]
        if len(document_text) > 500:
            preview += "\n\n*...(内容已截断，完整文稿见服务器文件)*"
        total_chars = len(document_text)

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "✍️ 文稿生成完毕"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**📌 写作指令**\n{instruction}\n\n"
                        f"**📄 字数统计：** 约 {total_chars} 字符\n"
                        f"**💾 保存路径：** `{save_path}`"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**📖 内容预览（前 500 字）**\n\n{preview}",
                },
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

                "**🔗 绑定专属多维表格（首次使用必做）**\n"
                "```\n/bind <app_token>\n```\n"
                "示例：`/bind bascXXXXXXXXXXXXXX`\n"
                "绑定您个人的飞书多维表格，实验数据将写入您的专属表格。\n"
                "绑定前请先在飞书创建空白多维表格，并将机器人添加为**可编辑**协作者。\n\n"

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

                "**🏷️ 给实验起别名**\n"
                "```\n/alias exp_<uuid> <别名>\n```\n"
                "示例：`/alias exp_19caeba9 MNIST基线实验`\n"
                "为实验设置简短易读名字，之后列表和报告卡片将显示该别名。\n\n"

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

                "**📈 成果可视化**\n"
                "用自然语言让大管家绘制 Loss/Accuracy 曲线等图表：\n"
                "示例：`帮我画出 exp_xxxxx 的 Loss 曲线`\n"
                "图表会自动发送到当前对话。\n\n"

                "**⏰ 定时任务**\n"
                "用自然语言设置定时汇报或定期检查：\n"
                "示例：`每天早上 9 点汇报所有实验进展`\n"
                "示例：`每 2 小时检查一下是否有实验异常`\n"
                "大管家会自动注册定时任务并在指定时间主动发送消息。\n\n"

                "**📝 撰写技术文稿**\n"
                "```\n/write <主题和要求> [exp_<uuid>]\n```\n"
                "示例：`/write 撰写一篇关于 Transformer 架构的技术综述`\n"
                "示例：`/write 基于实验结果写一篇技术报告 exp_19caeba9`\n"
                "AI 将生成排版专业的 Markdown 技术文稿。可选关联已有实验，自动读取其数据作为写作素材。\n"
                "<font color='grey'>文稿保存在服务器，也可自然语言触发：「帮我写一篇关于这次实验的技术报告」</font>\n\n"

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
        entries: list[dict],
        reply_message_id: str | None = None,
    ) -> str:
        """Send a card listing all existing experiments.

        Args:
            receive_id:      Feishu chat_id or open_id.
            receive_id_type: "chat_id" | "open_id" etc.
            entries:         List of dicts with keys: path (Path), task_id (str),
                             alias (str), status_icon (str). Sorted newest-first.

        Returns:
            Created message_id.
        """
        if not entries:
            body_elements = [{"tag": "markdown", "content": "_(暂无实验记录)_"}]
        else:
            body_elements = []
            for entry in entries:
                task_id_str: str = entry["task_id"]
                alias_str: str = entry["alias"]
                status_icon: str = entry["status_icon"]
                if alias_str != task_id_str:
                    display = f"**{alias_str}**\n<font color='grey'>{task_id_str}</font>"
                else:
                    display = f"`{task_id_str}`"
                body_elements.append({
                    "tag": "markdown",
                    "content": f"{status_icon} {display}",
                })
                body_elements.append({
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "进入会话"},
                            "type": "primary",
                            "value": {"key": "enter_session", "task_id": task_id_str},
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
