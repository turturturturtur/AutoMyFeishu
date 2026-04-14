"""Claude API client — agentic tool-use loop for experiment script generation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import anthropic

from claude_feishu_flow.ai.prompt import build_system_prompt, build_summarize_system_prompt
from claude_feishu_flow.ai.tools import ALL_TOOLS, handle_save_script

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 8  # safety cap; script generation typically finishes in 2 rounds


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
    ) -> str:
        """Ask Claude to generate plan.md and main.py, saving both via save_script.

        Runs the agentic tool-use loop until Claude saves main.py or
        exhausts max_rounds. Claude is expected to save plan.md first, then main.py.

        Args:
            user_text:     The user's instruction from Feishu.
            workspace_dir: Path to the experiment root directory (Experiments/exp_<uuid>/).
                           Files are saved to workspace_dir/setting/ by the handler.

        Returns:
            Absolute path of the saved main.py script.

        Raises:
            RuntimeError: If Claude never saves main.py within max_rounds.
        """
        messages: list[dict] = [{"role": "user", "content": user_text}]
        saved_files: dict[str, str] = {}  # filename -> abs_path

        for round_num in range(1, _MAX_ROUNDS + 1):
            logger.info("Claude round %d — sending %d messages", round_num, len(messages))

            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
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

            # Append Claude's full response to the conversation
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

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

                # Only exit once main.py has been saved (plan.md may or may not precede it)
                if "main.py" in saved_files:
                    logger.info("main.py saved; ending tool loop. saved_files=%s", list(saved_files.keys()))
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
            max_tokens=2048,
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
