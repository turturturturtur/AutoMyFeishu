"""Claude tool definitions for the experiment assistant.

Generation phase tools (ALL_TOOLS):
  save_script — write generated files (plan.md, main.py, run.sh) to the experiment directory.

Sub Agent tools (SUB_AGENT_TOOLS):
  read_realtime_log    — read tail of run.log (root or output/)
  save_script          — overwrite any file in the experiment directory
  restart_experiment   — signal the orchestrator to kill old process and restart with new code
  execute_bash_command — run shell commands inline
  send_local_image     — upload a local image file and send it to the Feishu chat
  sync_back_repo       — sync code changes back to the Storage master repo (safe whitelist filter)

Main Agent (Orchestrator) tools (MAIN_AGENT_TOOLS):
  execute_bash_command     — run shell commands inline
  list_experiments         — list experiment directories with metrics
  launch_experiment        — blocking: trigger new experiment pipeline (supports base_repo)
  edit_experiment          — blocking: trigger edit pipeline
  review_experiment        — blocking: trigger standalone code review (no execution)
  plot_experiment_metrics  — generate a matplotlib chart from run.log
  create_cron_job          — blocking: register a recurring scheduled task
  list_cron_jobs           — inline: list all active scheduled jobs
  cancel_cron_job          — inline: cancel a scheduled job by ID
  write_document           — blocking: draft a long Markdown document or technical report
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema (Anthropic tools API format)
# ---------------------------------------------------------------------------

SAVE_SCRIPT_TOOL: dict = {
    "name": "save_script",
    "description": (
        "Save a file to the experiment directory. "
        "For new blank experiments, use filename='plan.md' then filename='main.py' (saved under setting/). "
        "For repo-seeded experiments, write files directly to their proper path within the repo structure "
        "(e.g. filename='src/model.py'). "
        "Returns the absolute path of the saved file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Relative path of the file to save (e.g. 'plan.md', 'main.py', or 'src/train.py' for repo experiments).",
            },
            "code": {
                "type": "string",
                "description": "Complete file content.",
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
        "【重要】执行 Python 或 pip 命令前，请先检查 GLOBAL_RULES.md 中是否有关于虚拟环境的规定，"
        "并根据规定激活指定虚拟环境后再执行。"
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

SEND_LOCAL_IMAGE_TOOL: dict = {
    "name": "send_local_image",
    "description": (
        "当你在本地生成了图表、曲线图（如 .png / .jpg）后，必须主动调用此工具将图片发送给飞书聊天中的用户。"
        "不要让用户自己去服务器下载。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "图片的本地绝对路径，或相对实验目录（exp_dir）的相对路径。",
            }
        },
        "required": ["image_path"],
    },
}

SYNC_BACK_REPO_TOOL: dict = {
    "name": "sync_back_repo",
    "description": (
        "当你完成代码修改并在本地跑通了 Smoke Test 后，调用此工具将改动同步回 Storage 中的主仓库。"
        "只同步代码和配置文件（.py/.sh/.json/.yaml/.md 等），绝不写回 .pth/.ckpt/.log 等大文件。"
        "返回成功回写的文件列表。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "当前实验 ID，格式为 exp_<uuid>。",
            },
            "repo_name": {
                "type": "string",
                "description": "同 base_repo，支持传入绝对路径或 Storage 仓库名（与启动时的 base_repo 参数相同）。",
            },
        },
        "required": ["task_id", "repo_name"],
    },
}

SUB_AGENT_TOOLS: list[dict] = [READ_LOG_TOOL, SAVE_SCRIPT_TOOL, RESTART_EXPERIMENT_TOOL, EXECUTE_BASH_TOOL, SEND_LOCAL_IMAGE_TOOL, SYNC_BACK_REPO_TOOL]


# ---------------------------------------------------------------------------
# Main Agent (Orchestrator) tools
# ---------------------------------------------------------------------------

LIST_EXPERIMENTS_TOOL: dict = {
    "name": "list_experiments",
    "description": "列出所有已有的实验，返回实验 ID、状态和创建时间。用户询问「有哪些实验」「实验列表」时调用。",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

LAUNCH_EXPERIMENT_TOOL: dict = {
    "name": "launch_experiment",
    "description": (
        "启动一个全新的实验。当用户想运行/启动/做一个新实验时调用。"
        "调用后系统自动接管后续脚本生成和执行，无需其他操作。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "用户的实验需求描述，原样传入，不要改写或截断。",
            },
            "alias": {
                "type": "string",
                "description": "实验的简短易读别名，例如 'ViT_CIFAR10_baseline'。系统将用别名代替 UUID 显示。",
            },
            "base_repo": {
                "type": "string",
                "description": "如果要基于已有仓库实验，可以填入 Storage 下的仓库名（对应 Storage/<open_id>/<repo_name>/ 目录），或者直接填入宿主机上的绝对路径（如 /home/user/repo）。系统会将该仓库全量克隆到实验沙盒。留空则创建空白实验目录。",
            },
        },
        "required": ["instruction"],
    },
}

EDIT_EXPERIMENT_TOOL: dict = {
    "name": "edit_experiment",
    "description": (
        "修改一个已有的实验。需要明确指定 task_id（格式为 exp_<uuid>）和修改指令。"
        "调用后系统自动接管编辑流程，无需其他操作。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要修改的实验 ID，格式为 exp_<uuid>。",
            },
            "instruction": {
                "type": "string",
                "description": "对实验的修改指令，例如「将学习率改为 1e-4」。",
            },
        },
        "required": ["task_id", "instruction"],
    },
}

REVIEW_EXPERIMENT_TOOL: dict = {
    "name": "review_experiment",
    "description": (
        "对已生成但尚未执行（或已执行）的实验代码进行独立审阅、Bug 排查和优化，并返回审阅报告。"
        "当用户想单独审阅某个实验的代码质量时调用。"
        "调用后系统触发独立审阅任务，不会启动实验执行。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要审阅的实验 ID，格式为 exp_<uuid>。",
            },
        },
        "required": ["task_id"],
    },
}

PLOT_METRICS_TOOL: dict = {
    "name": "plot_experiment_metrics",
    "description": (
        "Execute a self-contained Python/matplotlib script to generate a chart from an experiment's "
        "output/run.log and save it to results/plot.png. The system will automatically send the "
        "generated image to the user. Use when the user asks for a loss curve, accuracy plot, or "
        "any visual chart from training logs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The experiment ID, e.g. exp_<uuid>.",
            },
            "python_code": {
                "type": "string",
                "description": (
                    "A self-contained Python script that reads output/run.log "
                    "(relative to the experiment root directory) and saves the chart to "
                    "results/plot.png. Must import matplotlib and call plt.savefig('results/plot.png')."
                ),
            },
        },
        "required": ["task_id", "python_code"],
    },
}

CREATE_CRON_JOB_TOOL: dict = {
    "name": "create_cron_job",
    "description": (
        "Register a recurring scheduled task. At each scheduled time, the system will automatically "
        "call list_experiments, summarize progress, and send a proactive message to the user. "
        "Use when the user says '每天X点汇报' or similar scheduling requests."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cron_expression": {
                "type": "string",
                "description": (
                    "Standard 5-field cron expression in local time: 'min hour day month weekday'. "
                    "Example: '0 9 * * *' = daily at 9am. '0 */2 * * *' = every 2 hours."
                ),
            },
            "task_description": {
                "type": "string",
                "description": "Human-readable description of what the cron job should do, in Chinese.",
            },
        },
        "required": ["cron_expression", "task_description"],
    },
}

LIST_CRON_JOBS_TOOL: dict = {
    "name": "list_cron_jobs",
    "description": "列出当前后台正在运行的所有定时任务的 ID、触发规则（cron 表达式）和任务描述。用户询问「有哪些定时任务」「已有的定时任务」时调用。",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

CANCEL_CRON_JOB_TOOL: dict = {
    "name": "cancel_cron_job",
    "description": "根据任务 ID 取消指定的定时任务。在取消之前，如果不知道 job_id，请先调用 list_cron_jobs 查找。",
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要取消的定时任务 ID（由 create_cron_job 返回，或通过 list_cron_jobs 查得）。",
            },
        },
        "required": ["job_id"],
    },
}

WRITE_DOCUMENT_TOOL: dict = {
    "name": "write_document",
    "description": (
        "根据用户意图撰写长篇 Markdown 技术文稿、论文草稿或技术报告。"
        "当用户想写文章、写报告、写论文、写文档时调用。"
        "如果文稿依赖某个已有实验的数据和结果，请提供 related_task_id。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "文稿的主题、要求和写作风格说明，原样传入，不要改写或截断。",
            },
            "related_task_id": {
                "type": "string",
                "description": (
                    "（可选）关联实验的 task_id，例如 exp_xxxxxxxx。"
                    "提供后系统将自动读取该实验的 plan.md/review.md/summary.md 作为写作素材。"
                ),
            },
        },
        "required": ["instruction"],
    },
}

RENAME_EXPERIMENT_TOOL: dict = {
    "name": "rename_experiment",
    "description": (
        "为已有实验设置或更新人类可读的别名。"
        "当用户想给实验起名字、重命名实验时调用。"
        "调用后系统立即更新别名，无需其他操作。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要重命名的实验 ID，格式为 exp_<uuid>。",
            },
            "new_alias": {
                "type": "string",
                "description": "新的人类可读别名，例如 'ResNet消融实验'。",
            },
        },
        "required": ["task_id", "new_alias"],
    },
}

MAIN_AGENT_TOOLS: list[dict] = [
    EXECUTE_BASH_TOOL,
    LIST_EXPERIMENTS_TOOL,
    LAUNCH_EXPERIMENT_TOOL,
    EDIT_EXPERIMENT_TOOL,
    REVIEW_EXPERIMENT_TOOL,
    PLOT_METRICS_TOOL,
    CREATE_CRON_JOB_TOOL,
    LIST_CRON_JOBS_TOOL,
    CANCEL_CRON_JOB_TOOL,
    WRITE_DOCUMENT_TOOL,
    RENAME_EXPERIMENT_TOOL,
]


@dataclasses.dataclass
class MainAgentResult:
    """Return value from chat_main_agent.

    text:               The model's conversational reply to send to the user.
                        Always present, even when an action is being taken.
    action_type:        "launch" | "edit" | "review" | "create_cron_job" | "write" | None.
                        When set, routes.py must start the corresponding pipeline.
    action_task_id:     Populated when action_type == "edit", "review", or "write" with a
                        related experiment.
    action_instruction: The instruction string for launch/edit/write pipelines, or
                        JSON params for create_cron_job.
    plot_path:          Absolute path to results/plot.png if plot_experiment_metrics
                        ran successfully; otherwise None.
    """

    text: str
    action_type: str | None = None
    action_task_id: str | None = None
    action_instruction: str | None = None
    plot_path: str | None = None
    action_alias: str | None = None
    action_base_repo: str | None = None


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
        schema = dict(tool["input_schema"])  # shallow copy to avoid mutating originals
        if "required" in schema and not schema["required"]:
            schema.pop("required")  # remove empty required list — some gateways reject it
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": schema,
            },
        })
    return result


# ---------------------------------------------------------------------------
# Experiment alias helper
# ---------------------------------------------------------------------------

def get_experiment_alias(exp_dir: Path) -> str:
    """Return the human-readable alias for an experiment, falling back to the directory name.

    Checks meta.json at the experiment root first, then legacy setting/meta.json.
    Falls back to exp_dir.name (e.g. "exp_<uuid>") if neither file has an alias.

    Args:
        exp_dir: The experiment root directory (e.g. Experiments/exp_<uuid>/).

    Returns:
        The alias string, or exp_dir.name as fallback.
    """
    import json as _json
    for meta_path in (exp_dir / "meta.json", exp_dir / "setting" / "meta.json"):
        try:
            if meta_path.exists():
                data = _json.loads(meta_path.read_text(encoding="utf-8"))
                alias = data.get("alias", "")
                if alias and alias.strip():
                    return alias.strip()
        except Exception:
            pass
    return exp_dir.name


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def handle_save_script(inputs: dict, experiment_dir: Path) -> str:
    """Write a file to the experiment directory.

    Routing logic:
    - If filename is a simple name (no path separator) like 'main.py' or 'plan.md',
      the file is saved under setting/ for backward compatibility with legacy experiments.
    - If filename contains a path separator (e.g. 'src/model.py', 'configs/train.yaml'),
      the file is saved relative to experiment_dir root, preserving the repo structure.

    When filename is 'main.py' (simple name, saved in setting/), any existing
    setting/run.sh is removed so the executor falls back to the correct launcher.

    Args:
        inputs:          The tool input dict from Claude (must contain 'filename' and 'code').
        experiment_dir:  The experiment root directory (Experiments/exp_<uuid>/).

    Returns:
        Absolute path of the saved file as a string.
    """
    filename: str = inputs["filename"]
    code: str = inputs["code"]

    if "/" in filename or "\\" in filename:
        # Repo-style path: write relative to experiment root
        script_path = experiment_dir / filename
        script_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Legacy simple filename: write under setting/
        setting_dir = experiment_dir / "setting"
        setting_dir.mkdir(parents=True, exist_ok=True)
        script_path = setting_dir / filename

        # When main.py is overwritten (e.g. reverting to single-GPU), remove any
        # stale run.sh so the executor doesn't keep using the old launcher.
        if filename == "main.py":
            run_sh = setting_dir / "run.sh"
            if run_sh.exists():
                run_sh.unlink()
                logger.info("save_script: removed stale run.sh (main.py was overwritten)")

    script_path.write_text(code, encoding="utf-8")
    abs_path = str(script_path.resolve())
    logger.info("save_script: wrote %d bytes to %s", len(code), abs_path)
    return abs_path


async def handle_read_log(inputs: dict, experiment_dir: Path) -> str:
    """Read the last N lines from the experiment's run.log.

    Checks experiment root first (new layout), then output/ subdir (legacy layout).

    Args:
        inputs:          Tool input dict from Claude (may contain 'n_lines').
        experiment_dir:  The experiment root directory (Experiments/exp_<uuid>/).

    Returns:
        The tail of the log file as a string, or a descriptive message if empty/missing.
    """
    n_lines: int = inputs.get("n_lines", 50)
    # Prefer root-level log (new layout), fall back to output/ (legacy)
    log_path = experiment_dir / "run.log"
    if not log_path.exists():
        log_path = experiment_dir / "output" / "run.log"

    if not log_path.exists():
        return f"Log file does not exist yet: {experiment_dir}/run.log"

    text = log_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return "Log file exists but is empty (experiment may not have started writing output yet)."

    lines = text.splitlines()
    tail = lines[-n_lines:] if len(lines) > n_lines else lines
    return "\n".join(tail)


async def handle_execute_bash(inputs: dict, exp_dir: Path) -> str:
    """Execute a shell command with cwd=exp_dir and return combined stdout+stderr (truncated to last 4000 chars).

    Args:
        inputs:       Tool input dict from Claude (must contain 'command').
        exp_dir:      The experiment root directory (used as cwd).

    Returns:
        Combined stdout+stderr output, or a timeout/error message.
    """
    command: str = inputs["command"]
    MAX_OUTPUT = 4000
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
            combined = f"[...截断，仅显示最后 {MAX_OUTPUT} 字符...]\n" + combined[-MAX_OUTPUT:]
        logger.info("execute_bash: command=%r returncode=%s", command, proc.returncode)
        return combined
    except Exception as e:
        return f"[执行失败] {e}"


async def handle_list_experiments(exp_base_dir: Path) -> str:
    """List experiment directories under exp_base_dir with purpose and live metrics.

    Returns a plain-text summary (newest first) that the model can read and describe.
    Includes experiment purpose (from plan.md), status, and live training metrics
    extracted from output/run.log when available.

    Args:
        exp_base_dir: The experiments root directory (e.g. Experiments/).

    Returns:
        Multi-line string with one entry per experiment, or a "no experiments" message.
    """
    import datetime
    import re

    if not exp_base_dir.exists():
        return "暂无实验记录（实验目录不存在）。"
    entries = sorted(
        (d for d in exp_base_dir.iterdir() if d.is_dir() and d.name.startswith("exp_")),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not entries:
        return "暂无实验记录。"

    lines: list[str] = []
    for d in entries:
        mtime = datetime.datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        status = "已完成" if (d / "results" / "summary.md").exists() else "未完成/运行中"

        # Extract experiment purpose from plan.md (first 80 non-empty chars)
        purpose = ""
        plan_path = d / "setting" / "plan.md"
        if plan_path.exists():
            try:
                plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
                # Strip leading markdown headers and whitespace
                cleaned = re.sub(r"^#+\s*", "", plan_text.strip(), flags=re.MULTILINE)
                first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), "")
                purpose = first_line[:80]
            except Exception:
                pass

        entry = f"- {d.name}  [{status}]  {mtime}"
        alias = get_experiment_alias(d)
        if alias != d.name:
            entry = f"- {alias} (ID: {d.name})  [{status}]  {mtime}"
        if purpose:
            entry += f"\n  目的: {purpose}"

        # Extract live metrics from run.log (last 200 lines)
        log_path = d / "output" / "run.log"
        if log_path.exists():
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                log_lines = log_text.splitlines()[-200:]
                tail = "\n".join(log_lines)

                metrics: dict[str, str] = {}

                # Epoch progress: "Epoch 3/10" or "epoch: 3"
                m = re.search(r"[Ee]poch[:\s]+(\d+)\s*/\s*(\d+)", tail)
                if not m:
                    m = re.search(r"[Ee]poch[:\s]+(\d+)", tail)
                if m:
                    metrics["Epoch"] = "/".join(m.groups()) if len(m.groups()) == 2 else m.group(1)

                # Loss: "loss: 0.1234" or "Loss=0.1234"
                m = re.search(r"[Ll]oss[=:\s]+([\d.]+(?:e[+-]?\d+)?)", tail)
                if m:
                    metrics["Loss"] = m.group(1)

                # Accuracy: "acc: 0.95" or "accuracy: 95.3%"
                m = re.search(r"(?:acc(?:uracy)?)[=:\s]+([\d.]+%?)", tail, re.IGNORECASE)
                if m:
                    metrics["Acc"] = m.group(1)

                # ETA: "ETA: 0:02:33" or "time left: 5 min"
                m = re.search(r"ETA[:\s]+([\d:]+)", tail, re.IGNORECASE)
                if not m:
                    m = re.search(r"time\s+left[:\s]+([\d]+\s*(?:min|s|sec|hour))", tail, re.IGNORECASE)
                if m:
                    metrics["ETA"] = m.group(1)

                if metrics:
                    metrics_str = "  ".join(f"{k}: {v}" for k, v in metrics.items())
                    entry += f"\n  Metrics: {metrics_str}"
            except Exception:
                pass

        lines.append(entry)

    return "\n".join(lines)


async def handle_rename_experiment(inputs: dict, exp_base_dir: Path) -> str:
    """Update the alias in setting/meta.json for the given experiment.

    Creates meta.json if it does not exist. Merges with existing data to avoid
    overwriting other keys.

    Args:
        inputs:       Tool input dict with keys "task_id" and "new_alias".
        exp_base_dir: Root directory containing all exp_<uuid> subdirectories.

    Returns:
        A confirmation string, or an error message if the experiment is not found.
    """
    import json as _json
    task_id: str = inputs["task_id"]
    new_alias: str = inputs["new_alias"].strip()
    exp_dir = exp_base_dir / task_id
    if not exp_dir.is_dir():
        return f"实验目录不存在: {task_id}"
    meta_path = exp_dir / "setting" / "meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    try:
        if meta_path.exists():
            data = _json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["alias"] = new_alias
    meta_path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("handle_rename_experiment: alias=%r task_id=%s", new_alias, task_id)
    return f"已将 {task_id} 的别名设置为：{new_alias}"


async def handle_plot_metrics(inputs: dict, exp_base_dir: Path) -> str:
    """Execute a matplotlib script in the experiment directory and save plot.png.

    The handler runs an agent-provided Python script inside the experiment's root
    directory, then checks that results/plot.png was created.

    Returns a sentinel "PLOT_READY:<abs_path>" on success, or an error string.

    Args:
        inputs:       Tool call inputs with keys "task_id" and "python_code".
        exp_base_dir: Root directory containing all exp_<uuid> subdirectories.
    """
    task_id: str = inputs["task_id"]
    code: str = inputs["python_code"]

    exp_dir = exp_base_dir / task_id
    if not exp_dir.is_dir():
        return f"实验目录不存在: {exp_dir}"

    results_dir = exp_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    script_path = results_dir / "_plot_script.py"
    script_path.write_text(code, encoding="utf-8")
    logger.info("handle_plot_metrics: running script %s", script_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            "python",
            str(script_path),
            cwd=str(exp_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        return "绘图脚本超时（60秒），请简化脚本或减小数据量。"
    except Exception as exc:
        return f"绘图脚本启动失败: {exc}"

    if proc.returncode != 0:
        err = stderr_bytes.decode("utf-8", errors="replace")[:2000]
        return f"绘图脚本运行失败 (exit {proc.returncode}):\n{err}"

    plot_path = results_dir / "plot.png"
    if not plot_path.exists():
        return "脚本已运行但 results/plot.png 未生成，请确认脚本调用了 plt.savefig('results/plot.png')。"

    logger.info("handle_plot_metrics: plot saved to %s", plot_path)
    return f"PLOT_READY:{plot_path}"


# ---------------------------------------------------------------------------
# sync_back_repo handler
# ---------------------------------------------------------------------------

# Extensions allowed to be written back to the Storage master repo
_SYNC_BACK_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sh", ".json", ".yaml", ".yml", ".md", ".txt",
    ".cfg", ".toml", ".ini", ".env", ".rst", ".ipynb",
})

# Patterns in relative path that are always skipped
_SYNC_BACK_BLOCKED_PARTS: tuple[str, ...] = (
    "__pycache__", ".git", ".pth", ".pt", ".ckpt", ".bin",
    ".log", ".h5", ".hdf5", ".npy", ".npz", ".pkl", ".safetensors",
)

# Max file size to write back (5 MB)
_SYNC_BACK_MAX_BYTES: int = 5 * 1024 * 1024


async def handle_sync_back(
    inputs: dict,
    exp_base_dir: Path,
    storage_dir: Path,
    open_id: str,
) -> str:
    """Sync code changes from an experiment sandbox back to the Storage master repo.

    Safety rules (all enforced, no exceptions):
    - Skip symlinks (never dereference large dataset links)
    - Skip files whose suffix is not in _SYNC_BACK_ALLOWED_EXTENSIONS
    - Skip files containing any blocked pattern in their relative path
    - Skip files larger than 5 MB
    - Explicitly create parent directories before copying (handles new subdirs)

    Args:
        inputs:       Tool call inputs with keys "task_id" and "repo_name".
        exp_base_dir: Root directory containing all exp_<uuid> subdirectories.
        storage_dir:  The Storage root (config.resolved_storage_dir()).
        open_id:      The user's Feishu open_id (for Storage/<open_id>/<repo_name>/).

    Returns:
        A summary string listing synced files or explaining why nothing was synced.
    """
    import shutil as _shutil

    task_id: str = inputs["task_id"]
    repo_name: str = inputs["repo_name"]

    exp_dir = exp_base_dir / task_id
    if not exp_dir.is_dir():
        return f"❌ 实验目录不存在: {exp_dir}"

    if repo_name.startswith("/"):
        storage_repo_dir = Path(repo_name)
    else:
        storage_repo_dir = storage_dir / open_id / repo_name
    if not storage_repo_dir.is_dir():
        return f"❌ 未找到仓库路径 '{repo_name}'（解析为: {storage_repo_dir}）。请确认路径正确。"

    synced: list[str] = []
    skipped_count = 0

    for src_path in exp_dir.rglob("*"):
        if not src_path.is_file():
            continue

        # Skip symlinks — never dereference (could point to massive datasets)
        if src_path.is_symlink():
            skipped_count += 1
            continue

        rel_path = src_path.relative_to(exp_dir)
        rel_str = str(rel_path)

        # Skip blocked patterns (binary weights, logs, caches, .git, etc.)
        if any(blocked in rel_str for blocked in _SYNC_BACK_BLOCKED_PARTS):
            skipped_count += 1
            continue

        # Skip non-whitelisted extensions
        if src_path.suffix.lower() not in _SYNC_BACK_ALLOWED_EXTENSIONS:
            skipped_count += 1
            continue

        # Skip files exceeding size limit
        try:
            size = src_path.stat().st_size
        except OSError:
            skipped_count += 1
            continue
        if size > _SYNC_BACK_MAX_BYTES:
            logger.warning("sync_back: skipping oversized file %s (%d bytes)", rel_str, size)
            skipped_count += 1
            continue

        # Copy to Storage, creating parent directories as needed
        dest_path = storage_repo_dir / rel_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(src_path, dest_path)
        synced.append(rel_str)
        logger.info("sync_back: %s → %s", src_path, dest_path)

    if not synced:
        return (
            f"⚠️ 没有文件被同步回 '{repo_name}'。"
            f"跳过了 {skipped_count} 个文件（不符合白名单或超过大小限制）。"
        )

    synced_list = "\n".join(f"  - {f}" for f in synced[:50])
    more = f"\n  ...以及另外 {len(synced) - 50} 个文件" if len(synced) > 50 else ""
    return (
        f"✅ 成功将 {len(synced)} 个文件同步回 Storage/{open_id}/{repo_name}/：\n"
        f"{synced_list}{more}\n"
        f"（已跳过 {skipped_count} 个文件：大文件/二进制/日志/软链接）"
    )
