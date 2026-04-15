"""Claude API client — agentic tool-use loop for experiment script generation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

import anthropic

from claude_feishu_flow.ai.prompt import (
    build_casual_chat_prompt,
    build_edit_chat_system_prompt,
    build_fix_system_prompt,
    build_main_agent_prompt,
    build_review_agent_prompt,
    build_sub_agent_system_prompt,
    build_system_prompt,
    build_summarize_system_prompt,
)
from claude_feishu_flow.ai.tools import ALL_TOOLS, EXECUTE_BASH_TOOL, MAIN_AGENT_TOOLS, SAVE_SCRIPT_TOOL, SUB_AGENT_TOOLS, handle_execute_bash, handle_list_experiments, handle_read_log, handle_save_script, MainAgentResult

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 30  # raised to accommodate max_tokens continuation rounds


@dataclass
class SubAgentResult:
    """Return value from chat_with_sub_agent.

    text:           Claude's reply to display to the user.
    needs_restart:  True when Sub Agent called restart_experiment — the caller
                    should immediately re-launch the experiment subprocess.
    """
    text: str
    needs_restart: bool = False


class ClaudeClient:
    """Wraps anthropic.AsyncAnthropic for the experiment-generation workflow.

    Supports both the official Anthropic API and third-party compatible endpoints
    (proxies, mirror sites, or OpenAI-compatible hosts running Claude-format models).

    Usage:
        # Official API
        client = ClaudeClient(api_key="sk-ant-...", model="claude-3-5-sonnet-latest")

        # Third-party mirror / proxy
        client = ClaudeClient(
            api_key="your-key",
            model="claude-3-5-sonnet-latest",
            base_url="https://your-proxy.example.com/v1",
        )

        script_path = await client.generate_experiment(
            user_text="帮我写一个打印 hello 的脚本",
            workspace_dir=Path("./Experiments/exp_<uuid>"),
        )
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-latest",
        base_url: Optional[str] = None,
    ) -> None:
        self._model = model
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)
        logger.info(
            "ClaudeClient ready — model=%s  base_url=%s",
            self._model,
            base_url or "(official Anthropic API)",
        )

    async def generate_experiment(
        self,
        user_text: str,
        workspace_dir: Path,
        is_edit_mode: bool = False,
        images: Optional[list[dict]] = None,
    ) -> str:
        """Ask Claude to generate plan.md and main.py, saving both via save_script.

        Runs the agentic tool-use loop until Claude saves main.py or
        exhausts max_rounds. Claude is expected to save plan.md first, then main.py.

        In edit mode, the existing plan.md and main.py are included in the prompt so
        Claude can modify them in-place rather than generating from scratch.

        Handles three stop_reason cases per round:
          - "end_turn"   : Claude finished naturally; exit loop.
          - "tool_use"   : Execute all tool_use blocks found in content, send results.
                           Also triggered when stop_reason is "max_tokens" but the
                           content still contains tool_use blocks (partial tool call).
          - "max_tokens" : No tool_use blocks — Claude was mid-text. Append a
                           continuation prompt so it finishes the current output.

        Args:
            user_text:     The user's instruction from Feishu.
            workspace_dir: Path to the experiment root directory (Experiments/exp_<uuid>/).
                           Files are saved to workspace_dir/setting/ by the handler.
            is_edit_mode:  If True, read existing plan.md / main.py and attach them so
                           Claude modifies rather than generates from scratch.

        Returns:
            Absolute path of the saved main.py script.

        Raises:
            RuntimeError: If Claude never saves main.py within max_rounds.
        """
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
            initial_content = "\n".join(context_parts)
        else:
            if images:
                content_blocks: list[dict] = []
                for img in images:
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["media_type"],
                            "data": img["base64_data"],
                        },
                    })
                content_blocks.append({"type": "text", "text": user_text})
                initial_content: list[dict] | str = content_blocks
            else:
                initial_content = user_text

        messages: list[dict] = [{"role": "user", "content": initial_content}]
        saved_files: dict[str, str] = {}  # filename -> abs_path

        for round_num in range(1, _MAX_ROUNDS + 1):
            logger.info("Claude round %d — sending %d messages", round_num, len(messages))

            response = await self._client.messages.create(
                model=self._model,
                max_tokens=8192,
                system=build_system_prompt(),
                tools=ALL_TOOLS,
                messages=messages,
            )

            logger.info(
                "Claude round %d — stop_reason=%s, %d content blocks",
                round_num,
                response.stop_reason,
                len(response.content),
            )

            # Always append Claude's full response to conversation history first
            messages.append({"role": "assistant", "content": response.content})

            # Collect any tool_use blocks regardless of stop_reason —
            # max_tokens can fire mid-tool-call, leaving a tool_use block
            # without a corresponding tool_result, which Anthropic rejects.
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if tool_use_blocks:
                # Execute every tool_use block and build tool_result list
                tool_results = []
                for block in tool_use_blocks:
                    tool_name: str = block.name
                    tool_input: dict = block.input
                    tool_use_id: str = block.id

                    logger.info("Tool call: %s filename=%s", tool_name, tool_input.get("filename"))

                    if tool_name == "save_script":
                        result_text = await handle_save_script(tool_input, workspace_dir)
                        saved_files[tool_input["filename"]] = result_text
                    else:
                        result_text = f"Unknown tool: {tool_name}"
                        logger.warning("Received unknown tool call: %s", tool_name)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_text,
                    })

                messages.append({"role": "user", "content": tool_results})

                if "main.py" in saved_files:
                    logger.info(
                        "main.py saved; ending tool loop. saved_files=%s",
                        list(saved_files.keys()),
                    )
                    break

            elif response.stop_reason == "max_tokens":
                # Pure text truncation — no tool calls. Ask Claude to continue.
                logger.warning(
                    "Round %d hit max_tokens with no tool_use blocks; sending continuation prompt",
                    round_num,
                )
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": "请继续"}],
                })

            else:
                # stop_reason == "end_turn" (or unexpected value) — nothing left to do
                break

        if "main.py" not in saved_files:
            raise RuntimeError(
                f"Claude did not save main.py within {_MAX_ROUNDS} rounds. "
                "Check the system prompt or model response."
            )

        return saved_files["main.py"]

    async def summarize_experiment(self, plan_text: str, log_text: str) -> str:
        """Ask Claude to summarize experiment results (no tool use).

        Args:
            plan_text: Content of plan.md (experiment design).
            log_text:  Truncated run + error logs (up to ~5000 chars).

        Returns:
            Markdown-formatted summary string.
        """
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=build_summarize_system_prompt(),
            messages=[{
                "role": "user",
                "content": (
                    f"## 实验计划 (Plan)\n\n{plan_text}\n\n"
                    f"## 运行日志 (Log)\n\n{log_text}"
                ),
            }],
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return "(Claude 未返回有效摘要)"

    _CASUAL_MAX_ROUNDS = 6

    async def chat_casual(
        self,
        user_text: str,
        images: list[dict] | None = None,
    ) -> str:
        """Single-turn casual chat with execute_bash_command tool support.

        Args:
            user_text: The user's message.
            images:    Optional list of {"media_type": ..., "base64_data": ...} dicts.

        Returns:
            The model's text reply.
        """
        content: list = []
        if images:
            for img in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img["media_type"],
                        "data": img["base64_data"],
                    },
                })
        content.append({"type": "text", "text": user_text})

        messages: list[dict] = [{"role": "user", "content": content}]
        system_prompt = build_casual_chat_prompt()
        cwd = Path(".")

        for round_num in range(1, self._CASUAL_MAX_ROUNDS + 1):
            logger.info("chat_casual round %d", round_num)
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system_prompt,
                tools=[EXECUTE_BASH_TOOL],
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks:
                # No tool calls — extract text and return
                for block in response.content:
                    if block.type == "text":
                        return block.text
                return "(未返回有效回复)"

            # Execute tool calls and feed results back
            tool_results = []
            for block in tool_use_blocks:
                try:
                    result_text = await handle_execute_bash(block.input, cwd)
                except Exception as exc:
                    result_text = f"Tool execution failed: {exc}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})

        # Fallback: return whatever text we have after max rounds
        if response is not None:
            for block in response.content:
                if block.type == "text":
                    return block.text
        return "(未返回有效回复)"

    _MAIN_AGENT_MAX_ROUNDS = 10

    async def chat_main_agent(
        self,
        user_text: str,
        exp_base_dir: Path,
        images: list[dict] | None = None,
    ) -> MainAgentResult:
        """Orchestrator agent: understands natural language and triggers experiment operations.

        Inline tools (executed locally, result fed back to model):
          - execute_bash_command
          - list_experiments

        Blocking tools (exit loop immediately, return action):
          - launch_experiment
          - edit_experiment

        Args:
            user_text:     The user's raw message.
            exp_base_dir:  Path to the experiments root directory (for list_experiments).
            images:        Optional list of {"media_type": ..., "base64_data": ...} dicts.

        Returns:
            MainAgentResult with the model's text reply and an optional action.
        """
        content: list = []
        if images:
            for img in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img["media_type"],
                        "data": img["base64_data"],
                    },
                })
        content.append({"type": "text", "text": user_text})

        messages: list[dict] = [{"role": "user", "content": content}]
        system_prompt = build_main_agent_prompt()
        response = None

        for round_num in range(1, self._MAIN_AGENT_MAX_ROUNDS + 1):
            logger.info("chat_main_agent round %d", round_num)
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system_prompt,
                tools=MAIN_AGENT_TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks:
                for block in response.content:
                    if block.type == "text":
                        return MainAgentResult(text=block.text)
                return MainAgentResult(text="(未返回有效回复)")

            # Extract any partial text reply that accompanies tool calls
            reply_text = ""
            for block in response.content:
                if block.type == "text":
                    reply_text += block.text

            tool_results: list[dict] = []
            action_result: MainAgentResult | None = None

            for block in tool_use_blocks:
                tool_name: str = block.name
                tool_input: dict = block.input

                if tool_name == "launch_experiment":
                    action_result = MainAgentResult(
                        text=reply_text or "好的，正在为你启动实验...",
                        action_type="launch",
                        action_instruction=tool_input.get("instruction", ""),
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "launch_experiment 已触发，系统将接管后续流程。",
                    })

                elif tool_name == "edit_experiment":
                    action_result = MainAgentResult(
                        text=reply_text or "好的，正在为你进入编辑流程...",
                        action_type="edit",
                        action_task_id=tool_input.get("task_id", ""),
                        action_instruction=tool_input.get("instruction", ""),
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "edit_experiment 已触发，系统将接管后续流程。",
                    })

                elif tool_name == "review_experiment":
                    action_result = MainAgentResult(
                        text=reply_text or "好的，正在为你启动代码审阅...",
                        action_type="review",
                        action_task_id=tool_input.get("task_id", ""),
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "review_experiment 已触发，系统将接管审阅流程。",
                    })

                elif tool_name == "execute_bash_command":
                    try:
                        result_text = await handle_execute_bash(block.input, Path("."))
                    except Exception as exc:
                        result_text = f"工具执行失败：{exc}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

                elif tool_name == "list_experiments":
                    try:
                        result_text = await handle_list_experiments(exp_base_dir)
                    except Exception as exc:
                        result_text = f"工具执行失败：{exc}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Unknown tool: {tool_name}",
                    })

            # Blocking tool found — exit immediately
            if action_result is not None:
                return action_result

            # Inline tools — feed results back and continue
            messages.append({"role": "user", "content": tool_results})

        # Fallback after max rounds
        if response is not None:
            for block in response.content:
                if block.type == "text":
                    return MainAgentResult(text=block.text)
        return MainAgentResult(text="(未返回有效回复)")

    async def chat_edit(
        self,
        exp_dir: Path,
        initial_instruction: str,
        user_queue: asyncio.Queue,
        reply_callback,  # async callable(text: str) -> None
    ) -> bool:
        """Interactive multi-turn conversation loop for editing an experiment.

        Drives a free-form dialogue between the user and Claude until Claude
        signals it is ready to run (by emitting [READY_TO_RUN] in its reply
        after saving main.py via save_script).

        Args:
            exp_dir:             Path to the experiment root (exp_<uuid>/).
            initial_instruction: The user's first /edit message text.
            user_queue:          asyncio.Queue fed by the webhook handler with
                                 subsequent user messages (str).
            reply_callback:      Async function to send Claude's text reply back
                                 to the Feishu chat.

        Returns:
            True if main.py was saved and Claude signalled READY_TO_RUN,
            False if the session was cancelled (/cancel or queue sentinel None).
        """
        plan_path = exp_dir / "setting" / "plan.md"
        main_path = exp_dir / "setting" / "main.py"

        # Build the opening context message
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
            # ── Call Claude ───────────────────────────────────────────────
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=build_edit_chat_system_prompt(),
                tools=ALL_TOOLS,
                messages=messages,
            )
            logger.info(
                "chat_edit: stop_reason=%s, %d content blocks",
                response.stop_reason, len(response.content),
            )

            messages.append({"role": "assistant", "content": response.content})

            # ── Handle tool_use blocks ────────────────────────────────────
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            if tool_use_blocks:
                for block in tool_use_blocks:
                    tool_name: str = block.name
                    tool_input: dict = block.input
                    if tool_name == "save_script":
                        result_text = await handle_save_script(tool_input, exp_dir)
                        if tool_input.get("filename") == "main.py":
                            saved_main = True
                        logger.info("chat_edit save_script: %s", tool_input.get("filename"))
                    else:
                        result_text = f"Unknown tool: {tool_name}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
                messages.append({"role": "user", "content": tool_results})

            # ── Extract text reply ────────────────────────────────────────
            reply_text = ""
            for block in response.content:
                if block.type == "text":
                    reply_text += block.text

            # Check for READY_TO_RUN signal
            ready = "[READY_TO_RUN]" in reply_text
            clean_reply = reply_text.replace("[READY_TO_RUN]", "").strip()

            if clean_reply:
                await reply_callback(clean_reply)

            # If tool_use blocks present and not yet done, send results then
            # continue the loop only if Claude didn't signal ready
            if tool_use_blocks and not ready:
                # Claude saved files but didn't signal ready — wait for more
                # API turns to finish (max_tokens continuation or end_turn)
                if response.stop_reason == "max_tokens":
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": "请继续"}],
                    })
                    continue
                # end_turn after tool processing without READY_TO_RUN:
                # Claude is awaiting user feedback; fall through to queue.get()

            if ready and saved_main:
                return True

            # ── Wait for next user message ────────────────────────────────
            try:
                next_msg: Optional[str] = await asyncio.wait_for(
                    user_queue.get(), timeout=300.0  # 5-minute idle timeout
                )
            except asyncio.TimeoutError:
                await reply_callback("⏰ 对话超时（5分钟无响应），编辑会话已结束。如需继续请重新发送 /edit 命令。")
                return False

            if next_msg is None:  # sentinel: user sent /cancel or session closed
                return False

            messages.append({"role": "user", "content": next_msg})

    async def fix_experiment(self, exp_dir: Path, error_log: str) -> str:
        """Ask Claude to debug and fix the failing main.py via save_script.

        Uses the same agentic tool-use loop as generate_experiment to preserve
        the tool_use/max_tokens anti-truncation invariant.

        Args:
            exp_dir:   Path to the experiment root (exp_<uuid>/).
            error_log: Captured stderr from the failed run.

        Returns:
            Absolute path of the fixed main.py.

        Raises:
            RuntimeError: If Claude does not save a fixed main.py within max_rounds.
        """
        main_path = exp_dir / "setting" / "main.py"
        current_code = main_path.read_text(encoding="utf-8") if main_path.exists() else ""

        initial_content = (
            f"运行报错信息：\n```\n{error_log}\n```\n\n"
            f"当前 main.py 代码：\n```python\n{current_code}\n```"
        )
        messages: list[dict] = [{"role": "user", "content": initial_content}]
        saved_files: dict[str, str] = {}

        for round_num in range(1, _MAX_ROUNDS + 1):
            logger.info("Claude fix round %d — sending %d messages", round_num, len(messages))

            response = await self._client.messages.create(
                model=self._model,
                max_tokens=8192,
                system=build_fix_system_prompt(),
                tools=ALL_TOOLS,
                messages=messages,
            )

            logger.info(
                "Claude fix round %d — stop_reason=%s, %d content blocks",
                round_num,
                response.stop_reason,
                len(response.content),
            )

            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if tool_use_blocks:
                tool_results = []
                for block in tool_use_blocks:
                    tool_name: str = block.name
                    tool_input: dict = block.input
                    tool_use_id: str = block.id

                    logger.info("Fix tool call: %s filename=%s", tool_name, tool_input.get("filename"))

                    if tool_name == "save_script":
                        result_text = await handle_save_script(tool_input, exp_dir)
                        saved_files[tool_input["filename"]] = result_text
                    else:
                        result_text = f"Unknown tool: {tool_name}"
                        logger.warning("Received unknown tool call: %s", tool_name)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_text,
                    })

                messages.append({"role": "user", "content": tool_results})

                if "main.py" in saved_files:
                    logger.info("fix_experiment: main.py saved at round %d", round_num)
                    break

            elif response.stop_reason == "max_tokens":
                logger.warning(
                    "Fix round %d hit max_tokens with no tool_use blocks; sending continuation prompt",
                    round_num,
                )
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": "请继续"}],
                })

            else:
                break

        if "main.py" not in saved_files:
            raise RuntimeError(
                f"Claude did not fix main.py within {_MAX_ROUNDS} rounds."
            )

        return saved_files["main.py"]

    # ------------------------------------------------------------------
    # Review Agent: static code review for generated experiments
    # ------------------------------------------------------------------

    _REVIEW_MAX_ROUNDS = 15

    async def review_experiment(self, exp_dir: Path, instruction: str) -> str:
        """Run Review Agent on generated plan.md + main.py; may auto-fix main.py via save_script.

        Args:
            exp_dir:     Path to the experiment root (exp_<uuid>/).
            instruction: The user's original experiment intent (used as review context).

        Returns:
            Review report text produced by the Review Agent.
        """
        plan_path = exp_dir / "setting" / "plan.md"
        script_path = exp_dir / "setting" / "main.py"
        plan_text = plan_path.read_text(encoding="utf-8") if plan_path.exists() else "(plan.md 不存在)"
        script_text = script_path.read_text(encoding="utf-8") if script_path.exists() else "(main.py 不存在)"

        system_prompt = build_review_agent_prompt()
        user_content = (
            f"【用户原始实验意图】\n{instruction}\n\n"
            f"【plan.md 内容】\n```markdown\n{plan_text}\n```\n\n"
            f"【main.py 内容】\n```python\n{script_text}\n```\n\n"
            "请开始审阅，如有问题请直接调用 save_script 修复，最后输出审阅报告。"
        )

        messages: list[dict] = [{"role": "user", "content": user_content}]
        review_report = ""

        for round_num in range(1, self._REVIEW_MAX_ROUNDS + 1):
            logger.info("review_experiment round %d", round_num)
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system_prompt,
                tools=[SAVE_SCRIPT_TOOL],
                messages=messages,
            )

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if text_blocks:
                review_report = text_blocks[-1].text

            if response.stop_reason == "end_turn" or not tool_calls:
                break

            if response.stop_reason == "max_tokens":
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": "请继续。"})
                continue

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict] = []
            for tc in tool_calls:
                try:
                    tool_result_str = await handle_save_script(tc.input, exp_dir)
                    logger.info("review_experiment: save_script called, filename=%s", tc.input.get("filename"))
                except Exception as exc:
                    tool_result_str = f"[工具调用失败] {exc}，请检查参数后重试。"
                    logger.warning("review_experiment: save_script failed: %s", exc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": tool_result_str,
                })
            messages.append({"role": "user", "content": tool_results})

        return review_report or "(审阅 Agent 未返回报告)"

    # ------------------------------------------------------------------
    # Sub Agent: per-experiment conversational assistant with log reading
    # ------------------------------------------------------------------

    _SUB_AGENT_MAX_ROUNDS = 15
    _SUB_AGENT_HISTORY_TRIM_THRESHOLD = 60
    _SUB_AGENT_HISTORY_KEEP = 40

    async def chat_with_sub_agent(
        self,
        task_id: str,
        user_text: str,
        exp_dir: Path,
        history: list[dict],
    ) -> SubAgentResult:
        """Handle a single turn of Sub Agent conversation.

        The conversation history is mutated in-place so it persists across
        calls (the caller stores it on Services.sub_agent_histories).

        Args:
            task_id:    Experiment ID (e.g. "exp_<uuid>").
            user_text:  The user's current message.
            exp_dir:    Path to the experiment root directory.
            history:    Mutable list of conversation messages (mutated in place).

        Returns:
            SubAgentResult with Claude's text reply and a needs_restart flag.
        """
        # Trim history to prevent unbounded growth
        if len(history) > self._SUB_AGENT_HISTORY_TRIM_THRESHOLD:
            del history[: len(history) - self._SUB_AGENT_HISTORY_KEEP]

        history.append({"role": "user", "content": user_text})

        system_prompt = build_sub_agent_system_prompt(task_id, str(exp_dir))
        response = None
        needs_restart = False

        for round_num in range(1, self._SUB_AGENT_MAX_ROUNDS + 1):
            logger.info("Sub agent round %d for task=%s", round_num, task_id)

            response = await self._client.messages.create(
                model=self._model,
                max_tokens=8192,
                system=system_prompt,
                tools=SUB_AGENT_TOOLS,
                messages=history,
            )

            history.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # If the response was cut off mid-generation (max_tokens) while trying
            # to write tool inputs, the block.input dict will be empty or missing
            # required keys.  Return a clear error for every truncated block so
            # Claude can retry with a shorter payload.
            if response.stop_reason == "max_tokens" and tool_use_blocks:
                trunc_results = [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            "Error: response was truncated before tool input was complete "
                            "(max_tokens limit reached). Please retry this tool call with "
                            "a shorter/chunked file content."
                        ),
                        "is_error": True,
                    }
                    for block in tool_use_blocks
                ]
                history.append({"role": "user", "content": trunc_results})
                logger.warning(
                    "Sub agent round %d truncated mid-tool-use for task=%s (%d blocks affected)",
                    round_num, task_id, len(tool_use_blocks),
                )
                continue

            if tool_use_blocks:
                tool_results = []
                for block in tool_use_blocks:
                    try:
                        if block.name == "read_realtime_log":
                            result_text = await handle_read_log(block.input, exp_dir)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            })
                        elif block.name == "save_script":
                            result_text = await handle_save_script(block.input, exp_dir)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            })
                        elif block.name == "restart_experiment":
                            needs_restart = True
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "重启信号已接收，请向用户回复确认消息。系统将在你回复后执行真正的重启。",
                            })
                        elif block.name == "execute_bash_command":
                            result_text = await handle_execute_bash(block.input, exp_dir)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            })
                        else:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Unknown tool: {block.name}",
                            })
                    except Exception as tool_exc:
                        logger.warning(
                            "Tool %r execution failed for task=%s: %s (input keys: %s)",
                            block.name, task_id, tool_exc, list(block.input.keys()) if block.input else "empty",
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": (
                                f"Tool execution failed: {tool_exc}. "
                                f"Received input keys: {list(block.input.keys()) if block.input else 'none'}. "
                                "Please retry with all required fields (filename and code)."
                            ),
                            "is_error": True,
                        })
                history.append({"role": "user", "content": tool_results})

                # If Claude signalled end_turn alongside tool use, break after
                # processing results (it will have text to show already).
                if response.stop_reason == "end_turn":
                    break
                continue

            # end_turn or max_tokens without tool use — done
            break

        # Extract text reply from the last response
        reply_parts: list[str] = []
        if response is not None:
            for block in response.content:
                if block.type == "text":
                    reply_parts.append(block.text)

        reply_text = "\n".join(reply_parts) if reply_parts else "操作已执行完毕。"
        return SubAgentResult(text=reply_text, needs_restart=needs_restart)
