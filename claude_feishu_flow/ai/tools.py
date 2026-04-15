"""Claude tool definitions for the experiment assistant.

Generation phase tools (ALL_TOOLS):
  save_script — write generated files (plan.md, main.py, run.sh) to the experiment's setting/ directory.

Sub Agent tools (SUB_AGENT_TOOLS):
  read_realtime_log    — read tail of output/run.log
  save_script          — overwrite any file under setting/ (same tool, different context)
  restart_experiment   — signal the orchestrator to kill old process and restart with new code
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema (Anthropic tools API format)
# ---------------------------------------------------------------------------

SAVE_SCRIPT_TOOL: dict = {
    "name": "save_script",
    "description": (
        "Save a file to the experiment's setting/ directory. "
        "You MUST call this tool twice: first with filename='plan.md' to write the experiment plan, "
        "then with filename='main.py' to write the executable Python script. "
        "Both files are saved under setting/ inside the experiment directory. "
        "Returns the absolute path of the saved file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Filename to save: 'plan.md' for the experiment plan, 'main.py' for the Python script.",
            },
            "code": {
                "type": "string",
                "description": "Complete file content: markdown text for plan.md, or Python source code for main.py.",
            },
        },
        "required": ["filename", "code"],
    },
}

ALL_TOOLS: list[dict] = [SAVE_SCRIPT_TOOL]

READ_LOG_TOOL: dict = {
    "name": "read_realtime_log",
    "description": (
        "Read the last N lines from the experiment's output/run.log file to check "
        "real-time output such as training loss, accuracy, epoch progress, or errors. "
        "Returns the tail of the log file as plain text. "
        "If the file does not exist or is empty, returns a descriptive message."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "n_lines": {
                "type": "integer",
                "description": "Number of lines to read from the end of the log file. Default 50.",
                "default": 50,
            },
        },
        "required": [],
    },
}

RESTART_EXPERIMENT_TOOL: dict = {
    "name": "restart_experiment",
    "description": (
        "终止当前正在运行的实验进程，并用最新的代码（setting/run.sh 或 setting/main.py）重新启动。"
        "请在调用 save_script 完成代码修改后，立刻调用此工具使修改生效。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要重启的实验 task_id（例如 exp_xxxxxxxx）。",
            },
        },
        "required": ["task_id"],
    },
}

EXECUTE_BASH_TOOL: dict = {
    "name": "execute_bash_command",
    "description": (
        "在宿主机执行系统终端命令并返回输出。可用于排查进程(ps aux)、检查 GPU (nvidia-smi)、"
        "查看依赖(pip list)或检查文件是否存在(ls -la)。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 bash 命令",
            }
        },
        "required": ["command"],
    },
}

SUB_AGENT_TOOLS: list[dict] = [READ_LOG_TOOL, SAVE_SCRIPT_TOOL, RESTART_EXPERIMENT_TOOL, EXECUTE_BASH_TOOL]


# ---------------------------------------------------------------------------
# OpenAI / Kimi tool schema conversion
# ---------------------------------------------------------------------------

def convert_to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI function-calling format.

    Anthropic tools use ``input_schema`` as the JSON Schema for parameters.
    OpenAI wraps each tool in ``{"type": "function", "function": {...}}`` with
    a ``parameters`` key instead.

    Args:
        anthropic_tools: List of tool dicts in Anthropic format (with ``input_schema``).

    Returns:
        List of tool dicts in OpenAI format.
    """
    result: list[dict] = []
    for tool in anthropic_tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        })
    return result


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def handle_save_script(inputs: dict, experiment_dir: Path) -> str:
    """Write the generated file to experiment_dir/setting/<filename>.

    When filename is 'main.py', any existing run.sh is removed so the executor
    falls back to `python3 setting/main.py`.  If the caller wants a custom
    launcher they must also save a run.sh explicitly.

    Args:
        inputs:          The tool input dict from Claude (must contain 'filename' and 'code').
        experiment_dir:  The experiment root directory (Experiments/exp_<uuid>/).
                         The setting/ subdirectory is created if it does not exist.

    Returns:
        Absolute path of the saved file as a string.
    """
    filename: str = inputs["filename"]
    code: str = inputs["code"]

    setting_dir = experiment_dir / "setting"
    setting_dir.mkdir(parents=True, exist_ok=True)
    script_path = setting_dir / filename
    script_path.write_text(code, encoding="utf-8")

    # When main.py is overwritten (e.g. reverting to single-GPU), remove any
    # stale run.sh so the executor doesn't keep using the old launcher.
    if filename == "main.py":
        run_sh = setting_dir / "run.sh"
        if run_sh.exists():
            run_sh.unlink()
            logger.info("save_script: removed stale run.sh (main.py was overwritten)")

    abs_path = str(script_path.resolve())
    logger.info("save_script: wrote %d bytes to %s", len(code), abs_path)
    return abs_path


async def handle_read_log(inputs: dict, experiment_dir: Path) -> str:
    """Read the last N lines from experiment_dir/output/run.log.

    Args:
        inputs:          Tool input dict from Claude (may contain 'n_lines').
        experiment_dir:  The experiment root directory (Experiments/exp_<uuid>/).

    Returns:
        The tail of the log file as a string, or a descriptive message if empty/missing.
    """
    n_lines: int = inputs.get("n_lines", 50)
    log_path = experiment_dir / "output" / "run.log"

    if not log_path.exists():
        return f"Log file does not exist yet: {log_path}"

    text = log_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return "Log file exists but is empty (experiment may not have started writing output yet)."

    lines = text.splitlines()
    tail = lines[-n_lines:] if len(lines) > n_lines else lines
    return "\n".join(tail)


async def handle_execute_bash(inputs: dict, exp_dir: Path) -> str:
    """Execute a shell command with cwd=exp_dir and return combined stdout+stderr (truncated to 2000 chars).

    Args:
        inputs:       Tool input dict from Claude (must contain 'command').
        exp_dir:      The experiment root directory (used as cwd).

    Returns:
        Combined stdout+stderr output, or a timeout/error message.
    """
    command: str = inputs["command"]
    MAX_OUTPUT = 2000
    TIMEOUT = 30

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(exp_dir),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"[超时] 命令在 {TIMEOUT}s 内未完成，已强制终止: {command}"

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        combined = stdout
        if stderr:
            combined = combined + ("\n" if combined else "") + "[stderr]\n" + stderr
        if not combined.strip():
            combined = f"[命令已执行，无输出] 退出码: {proc.returncode}"
        if len(combined) > MAX_OUTPUT:
            combined = combined[:MAX_OUTPUT] + f"\n... [输出已截断，共 {len(combined)} 字符]"
        logger.info("execute_bash: command=%r returncode=%s", command, proc.returncode)
        return combined
    except Exception as e:
        return f"[执行失败] {e}"
