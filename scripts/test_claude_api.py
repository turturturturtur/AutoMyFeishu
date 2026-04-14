"""Manual integration test for Step 6+7 — Claude Tool Use (generate_experiment).

Tests that ClaudeClient.generate_experiment():
  1. Sends the user instruction to Claude with the save_script tool.
  2. Claude generates Python code and calls save_script.
  3. The file is written to workspaces/task_<uuid>/main.py.
  4. The absolute path is returned.

Run:
    python scripts/test_claude_api.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

USER_INSTRUCTION = (
    "请帮我写一个输出 'Hello Claude Experiment' 的 Python 脚本，"
    "并在循环里 sleep 1 秒，共循环 3 次。"
)


async def main() -> None:
    import uuid
    from claude_feishu_flow.config import Config
    from claude_feishu_flow.ai.client import ClaudeClient

    try:
        config = Config()
    except Exception as e:
        print(f"[FAIL] Could not load Config: {e}")
        return

    if "xxx" in config.anthropic_api_key:
        print("[SKIP] ANTHROPIC_API_KEY not set in .env")
        return

    task_id = str(uuid.uuid4())
    workspace_dir = config.resolved_workspaces_dir() / f"task_{task_id}"

    print(f"\nTask ID : {task_id}")
    print(f"Workspace: {workspace_dir}")
    print(f"Instruction: {USER_INSTRUCTION}\n")

    client = ClaudeClient(api_key=config.anthropic_api_key, model=config.claude_model)

    print("=== ClaudeClient.generate_experiment ===")
    try:
        script_path = await client.generate_experiment(USER_INSTRUCTION, workspace_dir)
        print(f"[PASS] Script saved at: {script_path}")
    except Exception as e:
        print(f"[FAIL] generate_experiment: {e}")
        return

    # Verify file exists and is non-empty
    p = Path(script_path)
    if not p.exists():
        print(f"[FAIL] File does not exist: {script_path}")
        return

    content = p.read_text(encoding="utf-8")
    print(f"\n--- Generated script ({len(content)} bytes) ---")
    print(content)
    print("---")

    # Basic sanity checks
    checks = [
        ("file is non-empty",           len(content) > 0),
        ("contains 'Hello Claude'",      "Hello Claude" in content),
        ("contains 'sleep'",             "sleep" in content),
        ("workspace dir created",        workspace_dir.is_dir()),
        ("filename is main.py",          p.name == "main.py"),
    ]
    print("\n=== Sanity checks ===")
    all_ok = True
    for label, ok in checks:
        status = "[PASS]" if ok else "[FAIL]"
        print(f"{status} {label}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\nAll checks passed. You can run the script manually:")
        print(f"  python {script_path}")


if __name__ == "__main__":
    asyncio.run(main())
