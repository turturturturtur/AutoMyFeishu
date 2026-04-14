"""Claude tool definitions for the experiment assistant.

Only one tool is needed for the generation phase:
  save_script — write generated code to an isolated task workspace.
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
        "Save the generated Python experiment script to disk. "
        "Call this tool once you have finished writing the complete code. "
        "Returns the absolute path of the saved file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Filename for the script, e.g. 'main.py'.",
            },
            "code": {
                "type": "string",
                "description": "Complete, self-contained Python source code.",
            },
        },
        "required": ["filename", "code"],
    },
}

ALL_TOOLS: list[dict] = [SAVE_SCRIPT_TOOL]


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

async def handle_save_script(inputs: dict, workspace_dir: Path) -> str:
    """Write the generated code to workspace_dir/<filename>.

    Args:
        inputs:        The tool input dict from Claude (must contain 'filename' and 'code').
        workspace_dir: The task-specific directory (workspaces/task_<uuid>/).
                       Created if it does not exist.

    Returns:
        Absolute path of the saved file as a string.
    """
    filename: str = inputs["filename"]
    code: str = inputs["code"]

    workspace_dir.mkdir(parents=True, exist_ok=True)
    script_path = workspace_dir / filename
    script_path.write_text(code, encoding="utf-8")

    abs_path = str(script_path.resolve())
    logger.info("save_script: wrote %d bytes to %s", len(code), abs_path)
    return abs_path
