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
