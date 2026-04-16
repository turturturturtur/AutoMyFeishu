"""Web Dashboard routes: REST API + Jinja2 page rendering."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from claude_feishu_flow.ai.tools import get_experiment_alias
from claude_feishu_flow.server.app import Services

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

router = APIRouter()

_TAIL_BYTES = 100_000  # max bytes to read from end of each log file


# ── helpers ──────────────────────────────────────────────────────────────────

def _svc(request: Request) -> Services:
    return request.app.state.services


def _tail_file(path: Path) -> str:
    """Return the last _TAIL_BYTES bytes of *path* as a UTF-8 string.

    Never loads the whole file into memory, so multi-GB ML logs are safe.
    Returns "" when the file does not exist or is empty.
    """
    if not path.exists():
        return ""
    size = path.stat().st_size
    if size == 0:
        return ""
    with path.open("rb") as fh:
        fh.seek(-min(size, _TAIL_BYTES), 2)
        return fh.read().decode("utf-8", errors="replace")


def _sanitize_history(history: list[dict]) -> list[dict[str, str]]:
    """Convert a raw Claude/OpenAI message history to a simple [{role, content}] list.

    Handles:
    - str content → pass through
    - list content (multimodal blocks):
        - text block        → the text string
        - tool_use block    → "[调用工具: {name}]"
        - tool_result block → "[工具执行结果: {content[:200]}...]"
        - image block       → skipped entirely
    Joins multiple blocks with newline.
    """
    result: list[dict[str, str]] = []
    for msg in history:
        role = msg.get("role", "")
        raw = msg.get("content", "")
        if isinstance(raw, str):
            result.append({"role": role, "content": raw})
            continue
        if isinstance(raw, list):
            parts: list[str] = []
            for block in raw:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text)
                elif btype == "tool_use":
                    name = block.get("name", "unknown")
                    parts.append(f"[调用工具: {name}]")
                elif btype == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        # tool_result content may itself be a list of blocks
                        inner = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        content = inner
                    snippet = str(content)[:200]
                    parts.append(f"[工具执行结果: {snippet}...]")
                elif btype == "tool_calls":
                    # OpenAI-style tool_calls
                    name = block.get("function", {}).get("name", "unknown")
                    parts.append(f"[调用工具: {name}]")
                elif btype == "tool":
                    # OpenAI-style tool response role
                    snippet = str(block.get("content", ""))[:200]
                    parts.append(f"[工具执行结果: {snippet}...]")
                # image blocks → silently skipped
            result.append({"role": role, "content": "\n".join(parts)})
    return result


# ── page route ───────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# ── REST API ─────────────────────────────────────────────────────────────────

@router.get("/api/experiments")
async def list_experiments(request: Request) -> list[dict[str, Any]]:
    """Return metadata for all experiments, sorted newest-first."""
    svc = _svc(request)
    base_dir = svc.config.resolved_experiments_dir()
    experiments: list[dict[str, Any]] = []

    for exp_dir in sorted(base_dir.iterdir(), key=lambda p: p.stat().st_ctime, reverse=True):
        if not exp_dir.is_dir() or not exp_dir.name.startswith("exp_"):
            continue
        task_id = exp_dir.name
        alias = get_experiment_alias(exp_dir)

        # Derive status
        error_log = exp_dir / "output" / "error.log"
        summary = exp_dir / "results" / "summary.md"
        if task_id in svc.executor.active_processes:
            status = "Running"
        elif summary.exists():
            status = "Done"
        elif error_log.exists() and error_log.stat().st_size > 0:
            status = "Failed"
        else:
            status = "Pending"

        created_at = datetime.fromtimestamp(exp_dir.stat().st_ctime, tz=timezone.utc).isoformat()

        experiments.append({
            "task_id": task_id,
            "alias": alias,
            "status": status,
            "created_at": created_at,
            "has_plan": (exp_dir / "setting" / "plan.md").exists(),
            "has_script": (exp_dir / "setting" / "main.py").exists(),
            "has_review": (exp_dir / "setting" / "review.md").exists(),
            "has_summary": summary.exists(),
        })

    return experiments


@router.get("/api/experiments/{task_id}/logs")
async def get_logs(task_id: str, request: Request) -> dict[str, str]:
    """Return the last 100 KB of run.log and error.log for *task_id*."""
    svc = _svc(request)
    base_dir = svc.config.resolved_experiments_dir()
    exp_dir = base_dir / task_id

    if not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Experiment {task_id!r} not found")

    return {
        "run_log": _tail_file(exp_dir / "output" / "run.log"),
        "error_log": _tail_file(exp_dir / "output" / "error.log"),
    }


@router.get("/api/histories")
async def get_histories(request: Request) -> dict[str, Any]:
    """Return sanitized Main Agent and Sub Agent conversation histories."""
    svc = _svc(request)

    main_agent: dict[str, list[dict[str, str]]] = {
        chat_id: _sanitize_history(msgs)
        for chat_id, msgs in svc.main_agent_histories.items()
    }
    sub_agent: dict[str, list[dict[str, str]]] = {
        task_id: _sanitize_history(msgs)
        for task_id, msgs in svc.sub_agent_histories.items()
    }

    return {"main_agent": main_agent, "sub_agent": sub_agent}
