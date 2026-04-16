"""Web Dashboard routes: REST API + static HTML page."""

from __future__ import annotations

import logging
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import set_key as _dotenv_set_key
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from claude_feishu_flow.ai.client import ClaudeClient
from claude_feishu_flow.ai.kimi_client import KimiClient
from claude_feishu_flow.ai.tools import get_experiment_alias
from claude_feishu_flow.ai.token_tracker import get_tracker
from claude_feishu_flow.server.app import Services

logger = logging.getLogger(__name__)

_INDEX_HTML = Path(__file__).parent / "templates" / "index.html"

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

@router.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(_INDEX_HTML, media_type="text/html")


# ── REST API ─────────────────────────────────────────────────────────────────

@router.get("/api/experiments")
async def list_experiments(request: Request) -> list[dict[str, Any]]:
    """Return metadata for all experiments (all users), sorted newest-first.

    Supports two directory layouts:
    - New (multi-tenant): Experiments/<open_id>/exp_<uuid>/  → owner = open_id
    - Legacy (flat):      Experiments/exp_<uuid>/            → owner = "(legacy)"
    """
    svc = _svc(request)
    base_dir = svc.config.resolved_experiments_dir()
    experiments: list[dict[str, Any]] = []

    def _collect(exp_dir: Path, owner: str) -> None:
        task_id = exp_dir.name
        alias = get_experiment_alias(exp_dir)
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
            "owner": owner,
            "has_plan": (exp_dir / "setting" / "plan.md").exists(),
            "has_script": (exp_dir / "setting" / "main.py").exists(),
            "has_review": (exp_dir / "setting" / "review.md").exists(),
            "has_summary": summary.exists(),
        })

    if not base_dir.exists():
        return []

    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("exp_"):
            # Legacy flat layout: Experiments/exp_<uuid>/
            _collect(entry, "(legacy)")
        else:
            # New per-user layout: Experiments/<open_id>/exp_<uuid>/
            for sub in entry.iterdir():
                if sub.is_dir() and sub.name.startswith("exp_"):
                    _collect(sub, entry.name)

    experiments.sort(key=lambda e: e["created_at"], reverse=True)
    return experiments


@router.get("/api/experiments/{task_id}/logs")
async def get_logs(task_id: str, request: Request) -> dict[str, str]:
    """Return the last 100 KB of run.log and error.log for *task_id*.

    Searches both legacy flat layout and new per-user layout.
    """
    svc = _svc(request)
    base_dir = svc.config.resolved_experiments_dir()

    # Check legacy flat layout first
    flat_candidate = base_dir / task_id
    if flat_candidate.is_dir():
        exp_dir: Path = flat_candidate
    else:
        # Scan per-user subdirectories
        found: Path | None = None
        if base_dir.exists():
            for owner_dir in base_dir.iterdir():
                if owner_dir.is_dir() and not owner_dir.name.startswith("exp_"):
                    candidate = owner_dir / task_id
                    if candidate.is_dir():
                        found = candidate
                        break
        if found is None:
            raise HTTPException(status_code=404, detail=f"Experiment {task_id!r} not found")
        exp_dir = found

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


@router.get("/api/system_stats")
async def get_system_stats() -> dict[str, int]:
    """Return cumulative token usage across all AI API calls."""
    return get_tracker().get()


# ── settings constants ────────────────────────────────────────────────────────

_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "anthropic_api_key",
    "kimi_api_key",
    "feishu_app_secret",
    "feishu_verification_token",
    "feishu_encrypt_key",
})

_AI_RELOAD_KEYS: frozenset[str] = frozenset({
    "llm_provider",
    "anthropic_api_key",
    "anthropic_model",
    "anthropic_base_url",
    "kimi_api_key",
    "kimi_model",
    "kimi_base_url",
})

_ENV_FILE = Path(".env")


def _mask(value: str) -> str:
    """Return first4...last4 for non-empty secrets.

    Strings shorter than 9 chars get fully masked as '***' to avoid
    leaking the full value through the first4/last4 pattern.
    """
    if not value:
        return ""
    if len(value) < 9:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _coerce_value(field_name: str, raw: Any, cfg: Any) -> Any:
    """Convert a JSON value to the Config field's Python type.

    Handles str, int, and Optional[str] (empty string → None).
    """
    hints = typing.get_type_hints(type(cfg))
    hint = hints.get(field_name)
    if hint is None:
        return raw

    origin = getattr(hint, "__origin__", None)
    # Optional[str] is Union[str, None]
    if origin is typing.Union:
        inner_args = hint.__args__
        if type(None) in inner_args and raw == "":
            return None
        non_none = [a for a in inner_args if a is not type(None)]
        if non_none:
            return non_none[0](raw) if raw is not None else None
        return raw

    if hint is int:
        return int(raw)
    if hint is str:
        return str(raw) if raw is not None else ""
    return raw


def _reload_ai_client(svc: Services) -> None:
    """Re-instantiate the AI client from current svc.config and assign atomically.

    Python's GIL makes the attribute assignment atomic. In-flight requests that
    already hold a local reference to the old svc.ai complete safely with the
    old client. Both ClaudeClient and KimiClient are stateless between calls.
    """
    cfg = svc.config
    if cfg.llm_provider == "kimi":
        if not cfg.kimi_api_key:
            raise HTTPException(
                status_code=422,
                detail="llm_provider='kimi' 需要提供 kimi_api_key",
            )
        new_ai: Any = KimiClient(
            api_key=cfg.kimi_api_key,
            model=cfg.kimi_model,
            base_url=cfg.kimi_base_url,
        )
    else:
        if not cfg.anthropic_api_key:
            raise HTTPException(
                status_code=422,
                detail="llm_provider='anthropic' 需要提供 anthropic_api_key",
            )
        new_ai = ClaudeClient(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            base_url=cfg.anthropic_base_url or None,
        )
    svc.ai = new_ai
    # Clear all conversation histories: the two providers use incompatible
    # message formats (Anthropic ContentBlock objects vs plain OpenAI dicts).
    # Keeping old history across a provider switch would cause crashes or
    # identity leakage (Claude history being fed to Kimi or vice-versa).
    svc.main_agent_histories.clear()
    svc.sub_agent_histories.clear()
    logger.info(
        "AI client hot-reloaded: provider=%s model=%s — conversation histories cleared",
        cfg.llm_provider,
        getattr(new_ai, "_model", "?"),
    )


# ── settings endpoints ────────────────────────────────────────────────────────

@router.get("/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    """Return current Config with sensitive values masked as first4...last4."""
    cfg = _svc(request).config
    data: dict[str, Any] = {}
    for field_name in cfg.model_fields:
        raw = getattr(cfg, field_name)
        str_val = "" if raw is None else str(raw)
        if field_name in _SENSITIVE_KEYS:
            data[field_name] = _mask(str_val)
        else:
            data[field_name] = raw
    return data


@router.post("/api/settings")
async def update_settings(request: Request, payload: Dict[str, Any]) -> dict[str, str]:
    """Accept JSON overrides, persist to .env, hot-update svc.config and AI client.

    Rules:
    - Values containing '...' are skipped (the client echoed back a masked value).
    - Only fields declared in Config are accepted; unknown keys are ignored.
    - AI client is re-instantiated when any _AI_RELOAD_KEYS field changes value.
    """
    svc = _svc(request)
    cfg = svc.config
    known_fields = set(cfg.model_fields.keys())

    async with svc.settings_lock:
        ai_reload_needed = False
        errors: list[str] = []

        for field_name, new_value in payload.items():
            if field_name not in known_fields:
                continue
            # Skip masked values the frontend echoed back unchanged
            if isinstance(new_value, str) and "..." in new_value:
                continue

            try:
                coerced = _coerce_value(field_name, new_value, cfg)
            except (ValueError, TypeError) as exc:
                errors.append(f"{field_name}: {exc}")
                continue

            current_value = getattr(cfg, field_name)
            if coerced == current_value:
                continue

            # Persist to .env (dotenv uses uppercase key names)
            env_key = field_name.upper()
            dotenv_value = "" if coerced is None else str(coerced)
            _dotenv_set_key(_ENV_FILE, env_key, dotenv_value, quote_mode="never")

            # Mutate in-memory config (bypass pydantic's __setattr__ validator)
            object.__setattr__(cfg, field_name, coerced)
            logger.info("Config updated: %s = %r", field_name, coerced if field_name not in _SENSITIVE_KEYS else "***")

            if field_name in _AI_RELOAD_KEYS:
                ai_reload_needed = True

        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))

    if ai_reload_needed:
        _reload_ai_client(svc)

    return {"status": "ok"}


# ── cron job endpoints ────────────────────────────────────────────────────────

@router.get("/api/cron_jobs")
async def list_cron_jobs(request: Request) -> list[dict[str, Any]]:
    """Return all scheduled cron jobs with next_run_time."""
    return _svc(request).scheduler.get_jobs_for_api()


@router.post("/api/cron_jobs")
async def create_cron_job(request: Request, payload: Dict[str, Any]) -> dict[str, Any]:
    """Create a new cron job.

    Payload fields:
      cron_expression (str): 5-field cron, e.g. "0 9 * * *"
      instruction     (str): Task description / prompt for the AI agent
      chat_id         (str): Feishu chat_id to send results to
    """
    svc = _svc(request)
    cron_expr = str(payload.get("cron_expression") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    if not cron_expr or not instruction or not chat_id:
        raise HTTPException(status_code=422, detail="cron_expression, instruction, chat_id 均为必填")
    try:
        job_id = svc.scheduler.add_cron_job(cron_expr, instruction, chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"job_id": job_id, "status": "created"}


@router.put("/api/cron_jobs/{job_id}")
async def update_cron_job(job_id: str, request: Request, payload: Dict[str, Any]) -> dict[str, str]:
    """Update an existing cron job's trigger, instruction, and chat_id in-place."""
    svc = _svc(request)
    cron_expr = str(payload.get("cron_expression") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    if not cron_expr or not instruction or not chat_id:
        raise HTTPException(status_code=422, detail="cron_expression, instruction, chat_id 均为必填")
    try:
        svc.scheduler.update_cron_job(job_id, cron_expr, instruction, chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "updated", "job_id": job_id}


@router.delete("/api/cron_jobs/{job_id}")
async def delete_cron_job(job_id: str, request: Request) -> dict[str, str]:
    """Delete a cron job by job_id."""
    msg = _svc(request).scheduler.cancel_job(job_id)
    if "❌" in msg:
        raise HTTPException(status_code=404, detail=msg)
    return {"status": "deleted", "job_id": job_id}
