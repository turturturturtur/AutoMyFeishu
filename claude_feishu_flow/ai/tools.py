"""Claude tool definitions for the experiment assistant.

Only one tool is needed for the generation phase:
  save_script — write generated files (plan.md, main.py) to the experiment's setting/ directory.
"""

from __future__ import annotations

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

SUB_AGENT_TOOLS: list[dict] = [READ_LOG_TOOL]


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

async def handle_save_script(inputs: dict, experiment_dir: Path) -> str:
    """Write the generated file to experiment_dir/setting/<filename>.

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
