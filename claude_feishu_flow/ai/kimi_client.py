"""Kimi (Moonshot AI) client — OpenAI-compatible agentic tool-use loop.

Implements the same public interface as ClaudeClient so either can be injected
into Services.ai transparently.

Key API differences vs Anthropic:
  - Uses openai.AsyncOpenAI with base_url="https://api.moonshot.cn/v1"
  - System prompt goes in messages list as {"role": "system", ...}
  - Tool call detection: finish_reason == "tool_calls" (not "tool_use")
  - Tool call truncation: finish_reason == "length" (not "max_tokens")
  - Tool results: one {"role": "tool", "tool_call_id": ..., "content": ...} per call
  - Arguments in tc.function.arguments are a JSON string (must json.loads)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import openai

from claude_feishu_flow.ai.client import SubAgentResult
from claude_feishu_flow.ai.prompt import (
    build_edit_chat_system_prompt,
    build_fix_system_prompt,
    build_sub_agent_system_prompt,
    build_system_prompt,
    build_summarize_system_prompt,
)
from claude_feishu_flow.ai.tools import (
    ALL_TOOLS,
    SUB_AGENT_TOOLS,
    convert_to_openai_tools,
    handle_execute_bash,
    handle_read_log,
    handle_save_script,
)

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 12
_KIMI_BASE_URL = "https://api.moonshot.cn/v1"

# Pre-convert tool schemas once at import time
_ALL_OAI_TOOLS = convert_to_openai_tools(ALL_TOOLS)
_SUB_AGENT_OAI_TOOLS = convert_to_openai_tools(SUB_AGENT_TOOLS)


async def _dispatch_tool(name: str, tool_input: dict, exp_dir: Path) -> str:
    """Route a tool call by name to the appropriate handler."""
    if name == "save_script":
        return await handle_save_script(tool_input, exp_dir)
    elif name == "read_realtime_log":
        return await handle_read_log(tool_input, exp_dir)
    elif name == "execute_bash_command":
        return await handle_execute_bash(tool_input, exp_dir)
    else:
        return f"Unknown tool: {name}"


class KimiClient:
    """Kimi (Moonshot AI) backend with the same interface as ClaudeClient.

    Usage:
        client = KimiClient(api_key="sk-...", model="moonshot-v1-32k")
        script_path = await client.generate_experiment(
            user_text="帮我写一个打印 hello 的脚本",
            workspace_dir=Path("./Experiments/exp_<uuid>"),
        )
    """

    _SUB_AGENT_MAX_ROUNDS = 8
    _SUB_AGENT_HISTORY_TRIM_THRESHOLD = 60
    _SUB_AGENT_HISTORY_KEEP = 40

    def __init__(
        self,
        api_key: str,
        model: str = "moonshot-v1-32k",
        base_url: str = _KIMI_BASE_URL,
    ) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"User-Agent": "claude-code/0.1.0"},
        )
        logger.info("KimiClient ready — model=%s  base_url=%s", self._model, base_url)

    async def generate_experiment(
        self,
        user_text: str,
        workspace_dir: Path,
        is_edit_mode: bool = False,
        images: Optional[list[dict]] = None,
    ) -> str:
        """Generate plan.md and main.py via Kimi function-calling loop.

        Kimi does not support image inputs natively in the same way as Claude;
        if images are provided the text description is still sent.

        Returns:
            Absolute path of the saved main.py.

        Raises:
            RuntimeError: If Kimi never saves main.py within _MAX_ROUNDS.
        """
        system_prompt = build_system_prompt()

        if is_edit_mode:
            plan_path = workspace_dir / "setting" / "plan.md"
            main_path = workspace_dir / "setting" / "main.py"
            context_parts: list[str] = [
                f"用户的修改指令：{user_text}",
                "",
                "以下是现有的实验代码，请根据用户指令修改，并调用 save_script 覆盖保存。",
            ]
            if plan_path.exists():
                context_parts.append(
                    f"\n## 现有 plan.md\n\n```markdown\n{plan_path.read_text(encoding='utf-8')}\n```"
                )
            if main_path.exists():
                context_parts.append(
                    f"\n## 现有 main.py\n\n```python\n{main_path.read_text(encoding='utf-8')}\n```"
                )
            initial_text = "\n".join(context_parts)
        else:
            initial_text = user_text
            if images:
                # Prepend a note for image-based requests; Kimi doesn't support
                # inline base64 images in the same multimodal format as Claude.
                initial_text = "(用户发送了图片，请参考图片内容生成实验脚本)" if not user_text else user_text

        messages: list[dict] = [{"role": "user", "content": initial_text}]
        saved_files: dict[str, str] = {}

        for round_num in range(1, _MAX_ROUNDS + 1):
            logger.info("Kimi round %d — sending %d messages", round_num, len(messages))

            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=8192,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=_ALL_OAI_TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            logger.info(
                "Kimi round %d — finish_reason=%s tool_calls=%s",
                round_num,
                finish_reason,
                len(msg.tool_calls) if msg.tool_calls else 0,
            )

            # Append assistant turn (preserve tool_calls in dict form)
            messages.append(msg.model_dump(exclude_unset=True))

            if msg.tool_calls:
                # Response truncated mid-tool-call — return error so Kimi retries
                if finish_reason == "length":
                    for tc in msg.tool_calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": (
                                "Error: response was truncated before tool input was complete "
                                "(length limit reached). Please retry with shorter/chunked content."
                            ),
                        })
                    logger.warning("Kimi round %d truncated mid-tool-call, retrying", round_num)
                    continue

                # Execute each tool call — wrap in try/except for fault tolerance
                for tc in msg.tool_calls:
                    try:
                        tool_input: dict = json.loads(tc.function.arguments)
                        result_text = await _dispatch_tool(tc.function.name, tool_input, workspace_dir)
                        if tc.function.name == "save_script" and tool_input.get("filename") == "main.py":
                            saved_files["main.py"] = result_text
                        logger.info("Tool call: %s filename=%s", tc.function.name, tool_input.get("filename"))
                    except Exception as e:
                        result_text = (
                            f"Tool execution failed: {e}. "
                            f"Please retry with all required fields."
                        )
                        logger.warning("Tool %r failed: %s", tc.function.name, e)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

                if "main.py" in saved_files:
                    logger.info("main.py saved; ending tool loop. saved_files=%s", list(saved_files.keys()))
                    break

            elif finish_reason == "length":
                # Pure text truncation — send continuation
                logger.warning("Round %d hit length with no tool_calls; sending continuation", round_num)
                messages.append({"role": "user", "content": "请继续"})

            else:
                # finish_reason == "stop"
                break

        if "main.py" not in saved_files:
            raise RuntimeError(
                f"Kimi did not save main.py within {_MAX_ROUNDS} rounds. "
                "Check the system prompt or model response."
            )

        return saved_files["main.py"]

    async def summarize_experiment(self, plan_text: str, log_text: str) -> str:
        """Summarize experiment results (no tool use).

        Returns:
            Markdown-formatted summary string.
        """
        response = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": build_summarize_system_prompt()},
                {
                    "role": "user",
                    "content": (
                        f"## 实验计划 (Plan)\n\n{plan_text}\n\n"
                        f"## 运行日志 (Log)\n\n{log_text}"
                    ),
                },
            ],
        )
        content = response.choices[0].message.content
        return content if content else "(Kimi 未返回有效摘要)"

    async def chat_edit(
        self,
        exp_dir: Path,
        initial_instruction: str,
        user_queue: asyncio.Queue,
        reply_callback,  # async callable(text: str) -> None
    ) -> bool:
        """Interactive multi-turn conversation loop for editing an experiment.

        Returns:
            True if main.py was saved and [READY_TO_RUN] was signalled.
            False if cancelled or timed out.
        """
        system_prompt = build_edit_chat_system_prompt()
        plan_path = exp_dir / "setting" / "plan.md"
        main_path = exp_dir / "setting" / "main.py"

        context_parts: list[str] = [
            f"用户的修改需求：{initial_instruction}",
            "",
            "以下是当前实验文件，供你参考：",
        ]
        if plan_path.exists():
            context_parts.append(
                f"\n## plan.md\n\n```markdown\n{plan_path.read_text(encoding='utf-8')}\n```"
            )
        if main_path.exists():
            context_parts.append(
                f"\n## main.py\n\n```python\n{main_path.read_text(encoding='utf-8')}\n```"
            )

        messages: list[dict] = [{"role": "user", "content": "\n".join(context_parts)}]
        saved_main = False

        while True:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=4096,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=_ALL_OAI_TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            logger.info("chat_edit: finish_reason=%s tool_calls=%s",
                        finish_reason, len(msg.tool_calls) if msg.tool_calls else 0)

            messages.append(msg.model_dump(exclude_unset=True))

            # Handle tool calls
            if msg.tool_calls:
                if finish_reason == "length":
                    for tc in msg.tool_calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "Error: truncated before tool input complete. Retry with shorter content.",
                        })
                    continue

                for tc in msg.tool_calls:
                    try:
                        tool_input = json.loads(tc.function.arguments)
                        result_text = await _dispatch_tool(tc.function.name, tool_input, exp_dir)
                        if tc.function.name == "save_script" and tool_input.get("filename") == "main.py":
                            saved_main = True
                        logger.info("chat_edit tool: %s filename=%s", tc.function.name, tool_input.get("filename"))
                    except Exception as e:
                        result_text = f"Tool execution failed: {e}. Please retry with all required fields."
                        logger.warning("chat_edit tool %r failed: %s", tc.function.name, e)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

            # Extract text reply and check for READY_TO_RUN
            reply_text = msg.content or ""
            ready = "[READY_TO_RUN]" in reply_text
            clean_reply = reply_text.replace("[READY_TO_RUN]", "").strip()

            if clean_reply:
                await reply_callback(clean_reply)

            if msg.tool_calls and not ready:
                if finish_reason == "length":
                    messages.append({"role": "user", "content": "请继续"})
                    continue

            if ready and saved_main:
                return True

            # Wait for next user message (5-minute idle timeout)
            try:
                next_msg: Optional[str] = await asyncio.wait_for(
                    user_queue.get(), timeout=300.0
                )
            except asyncio.TimeoutError:
                await reply_callback("⏰ 对话超时（5分钟无响应），编辑会话已结束。如需继续请重新发送 /edit 命令。")
                return False

            if next_msg is None:
                return False

            messages.append({"role": "user", "content": next_msg})

    async def fix_experiment(self, exp_dir: Path, error_log: str) -> str:
        """Debug and fix a failing main.py via tool-use loop.

        Returns:
            Absolute path of the fixed main.py.

        Raises:
            RuntimeError: If Kimi does not save a fixed main.py within _MAX_ROUNDS.
        """
        system_prompt = build_fix_system_prompt()
        main_path = exp_dir / "setting" / "main.py"
        current_code = main_path.read_text(encoding="utf-8") if main_path.exists() else ""

        messages: list[dict] = [{
            "role": "user",
            "content": (
                f"运行报错信息：\n```\n{error_log}\n```\n\n"
                f"当前 main.py 代码：\n```python\n{current_code}\n```"
            ),
        }]
        saved_files: dict[str, str] = {}

        for round_num in range(1, _MAX_ROUNDS + 1):
            logger.info("Kimi fix round %d — sending %d messages", round_num, len(messages))

            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=8192,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=_ALL_OAI_TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            logger.info("Kimi fix round %d — finish_reason=%s", round_num, finish_reason)

            messages.append(msg.model_dump(exclude_unset=True))

            if msg.tool_calls:
                if finish_reason == "length":
                    for tc in msg.tool_calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": (
                                "Error: truncated before tool input complete. "
                                "Retry with shorter/chunked content."
                            ),
                        })
                    logger.warning("Kimi fix round %d truncated mid-tool-call, retrying", round_num)
                    continue

                for tc in msg.tool_calls:
                    try:
                        tool_input = json.loads(tc.function.arguments)
                        result_text = await _dispatch_tool(tc.function.name, tool_input, exp_dir)
                        if tc.function.name == "save_script" and tool_input.get("filename") == "main.py":
                            saved_files["main.py"] = result_text
                        logger.info("Fix tool call: %s filename=%s", tc.function.name, tool_input.get("filename"))
                    except Exception as e:
                        result_text = f"Tool execution failed: {e}. Please retry with all required fields."
                        logger.warning("Fix tool %r failed: %s", tc.function.name, e)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

                if "main.py" in saved_files:
                    logger.info("fix_experiment: main.py saved at round %d", round_num)
                    break

            elif finish_reason == "length":
                logger.warning("Fix round %d hit length with no tool_calls; sending continuation", round_num)
                messages.append({"role": "user", "content": "请继续"})

            else:
                break

        if "main.py" not in saved_files:
            raise RuntimeError(f"Kimi did not fix main.py within {_MAX_ROUNDS} rounds.")

        return saved_files["main.py"]

    async def chat_with_sub_agent(
        self,
        task_id: str,
        user_text: str,
        exp_dir: Path,
        history: list[dict],
    ) -> SubAgentResult:
        """Handle a single turn of Sub Agent conversation.

        History is mutated in-place (OpenAI-format dicts) across calls.

        Returns:
            SubAgentResult with Kimi's text reply and a needs_restart flag.
        """
        # Trim history to prevent unbounded growth
        if len(history) > self._SUB_AGENT_HISTORY_TRIM_THRESHOLD:
            del history[: len(history) - self._SUB_AGENT_HISTORY_KEEP]

        history.append({"role": "user", "content": user_text})

        system_prompt = build_sub_agent_system_prompt(task_id, str(exp_dir))
        response = None
        needs_restart = False

        for round_num in range(1, self._SUB_AGENT_MAX_ROUNDS + 1):
            logger.info("Kimi sub agent round %d for task=%s", round_num, task_id)

            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=8192,
                messages=[{"role": "system", "content": system_prompt}] + history,
                tools=_SUB_AGENT_OAI_TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            history.append(msg.model_dump(exclude_unset=True))

            tool_calls = msg.tool_calls or []

            # Truncated mid-tool-call: return error for each block so Kimi retries
            if finish_reason == "length" and tool_calls:
                for tc in tool_calls:
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            "Error: response was truncated before tool input was complete "
                            "(length limit reached). Please retry this tool call with "
                            "a shorter/chunked file content."
                        ),
                    })
                logger.warning(
                    "Sub agent round %d truncated mid-tool-use for task=%s (%d blocks affected)",
                    round_num, task_id, len(tool_calls),
                )
                continue

            if tool_calls:
                for tc in tool_calls:
                    try:
                        tool_input = json.loads(tc.function.arguments)
                        if tc.function.name == "restart_experiment":
                            needs_restart = True
                            result_text = "重启信号已接收，请向用户回复确认消息。系统将在你回复后执行真正的重启。"
                        else:
                            result_text = await _dispatch_tool(tc.function.name, tool_input, exp_dir)
                    except Exception as tool_exc:
                        logger.warning(
                            "Sub agent tool %r failed for task=%s: %s (input keys: %s)",
                            tc.function.name, task_id, tool_exc,
                            list(json.loads(tc.function.arguments).keys()) if tc.function.arguments else "empty",
                        )
                        result_text = (
                            f"Tool execution failed: {tool_exc}. "
                            "Please retry with all required fields."
                        )
                    history.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})

                if finish_reason == "stop":
                    break
                continue

            # stop or length without tool calls — done
            break

        # Extract text reply from the last response
        reply_text = ""
        if response is not None:
            reply_text = response.choices[0].message.content or ""

        return SubAgentResult(text=reply_text or "操作已执行完毕。", needs_restart=needs_restart)
