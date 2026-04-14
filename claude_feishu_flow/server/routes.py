"""FastAPI webhook route and background pipeline handler."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from claude_feishu_flow.feishu.webhook import parse_webhook_event, verify_feishu_signature_v2, decrypt_feishu_message

logger = logging.getLogger(__name__)
router = APIRouter()

_LOG_TAIL_RUN = 4000    # chars from run.log tail fed to Claude
_LOG_TAIL_ERR = 1000    # chars from error.log tail fed to Claude
_PLAN_CARD_MAX = 500    # chars of plan.md shown in card summary


def _tail_file(path: Path, max_chars: int) -> str:
    """Read the last *max_chars* characters of a file; return '' if missing."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:] if len(text) > max_chars else text
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Webhook entry point — must return HTTP 200 within ~50ms
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def feishu_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Receive Feishu webhook events.

    Guarantees HTTP 200 within milliseconds by:
    1. Verifying the signature (pure CPU, <1ms).
    2. Parsing the event (memory-only, <1ms).
    3. Checking for duplicate message_ids (O(1) set lookup).
    4. Registering the heavy work as a BackgroundTask — runs AFTER the
       response socket is flushed, so Feishu never times out waiting.
    """
    body_bytes = await request.body()
    svc = request.app.state.services

    # ── Signature verification ────────────────────────────────────────────
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    signature = request.headers.get("X-Lark-Signature", "")

    if signature:
        # When encryption is enabled, Feishu signs with encrypt_key; otherwise
        # it uses the verification_token. Use whichever is non-empty.
        sig_token = svc.config.feishu_encrypt_key or svc.config.feishu_verification_token
        if not verify_feishu_signature_v2(timestamp, nonce, body_bytes, sig_token, signature):
            logger.warning("Webhook signature verification failed")
            return JSONResponse({"code": 40001, "msg": "invalid signature"}, status_code=401)

    # ── Event parsing ─────────────────────────────────────────────────────
    try:
        raw = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse({"code": 40002, "msg": "invalid json"}, status_code=400)

    # ── AES-256-CBC decryption (when Feishu encryption is enabled) ────────
    if "encrypt" in raw:
        encrypt_key = svc.config.feishu_encrypt_key
        if not encrypt_key:
            logger.warning("Received encrypted webhook but FEISHU_ENCRYPT_KEY is not configured")
            return JSONResponse({"code": 40003, "msg": "encryption not configured"}, status_code=400)
        try:
            raw = decrypt_feishu_message(raw["encrypt"], encrypt_key)
        except Exception as exc:
            logger.warning("Failed to decrypt webhook body: %s", exc)
            return JSONResponse({"code": 40004, "msg": "decryption failed"}, status_code=400)

    event = parse_webhook_event(raw)

    # ── URL verification challenge (one-time setup) ───────────────────────
    if event.event_type == "url_verification":
        logger.info("Responding to url_verification challenge")
        return JSONResponse({"challenge": event.challenge})

    # ── Only handle message events ────────────────────────────────────────
    if event.event_type != "im.message.receive_v1":
        return JSONResponse({"code": 0, "msg": "ignored"})

    if not event.text or not event.text.strip():
        return JSONResponse({"code": 0, "msg": "empty message ignored"})

    # ── Deduplication: Feishu retries on timeout, avoid double-processing ─
    if event.message_id and event.message_id in svc.processing_ids:
        logger.info("Duplicate message_id=%s, skipping", event.message_id)
        return JSONResponse({"code": 0, "msg": "duplicate"})

    if event.message_id:
        svc.processing_ids.add(event.message_id)

    # ── Register background task and IMMEDIATELY return 200 ───────────────
    background_tasks.add_task(_handle_message, event=event, svc=svc)
    return JSONResponse({"code": 0, "msg": "accepted"})


# ---------------------------------------------------------------------------
# Background pipeline — runs after HTTP 200 has been sent to Feishu
# ---------------------------------------------------------------------------

async def _handle_message(event, svc) -> None:  # type: ignore[no-untyped-def]
    """Full two-phase pipeline:

    Phase A (Generation) — Claude generates plan.md and main.py in setting/.
    Phase B (Execution)  — Python runs setting/main.py, writes logs to output/.
    Phase C (Reporting)  — Results written to Bitable, user notified.
    """
    chat_id: str = event.chat_id or ""
    user_text: str = event.text or ""
    task_id = f"exp_{uuid.uuid4()}"
    exp_dir = svc.config.resolved_experiments_dir() / task_id

    # Create the three-level directory structure upfront
    for sub in ("setting", "output", "results"):
        (exp_dir / sub).mkdir(parents=True, exist_ok=True)

    logger.info("Background task started task_id=%s chat_id=%s exp_dir=%s", task_id, chat_id, exp_dir)

    async def notify(text: str) -> None:
        try:
            await svc.messaging.send_text(chat_id, text)
        except Exception as exc:
            logger.warning("Failed to send notification: %s", exc)

    try:
        # ── Phase A: Generation ───────────────────────────────────────────
        await notify("正在理解需求，生成实验计划和脚本，请稍候...")

        script_path = await svc.claude.generate_experiment(
            user_text=user_text,
            workspace_dir=exp_dir,
        )
        logger.info("Phase A complete: script_path=%s", script_path)

        # ── Phase B: Execution ────────────────────────────────────────────
        await notify(
            f"实验脚本已生成，正在后台执行...\n"
            f"实验 ID：{task_id}\n"
            f"计划文件：{exp_dir / 'setting' / 'plan.md'}"
        )

        result = await svc.executor.run(exp_dir)

        status = "success" if result.success else "failed"
        logger.info(
            "Phase B complete: status=%s returncode=%d duration=%.2fs",
            status, result.returncode, result.duration_seconds,
        )

        # ── Phase C: AI Analysis + Card ───────────────────────────────────
        await notify("正在用 AI 分析实验结果，请稍候...")

        plan_path = exp_dir / "setting" / "plan.md"
        plan_text = _tail_file(plan_path, 99999)  # full plan for Claude

        run_log = _tail_file(exp_dir / "output" / "run.log", _LOG_TAIL_RUN)
        err_log = _tail_file(exp_dir / "output" / "error.log", _LOG_TAIL_ERR)
        log_text = ""
        if run_log:
            log_text += f"### stdout (run.log)\n{run_log}\n\n"
        if err_log:
            log_text += f"### stderr (error.log)\n{err_log}\n"
        if not log_text:
            log_text = "(无日志输出)"

        summary_md = await svc.claude.summarize_experiment(plan_text, log_text)
        logger.info("Phase C: AI summary generated (%d chars)", len(summary_md))

        # Write summary to results/summary.md
        summary_path = exp_dir / "results" / "summary.md"
        summary_path.write_text(summary_md, encoding="utf-8")
        logger.info("Phase C: summary written to %s", summary_path)

        # Write to Bitable
        record_id = await svc.bitable.append_record({
            "Command":       user_text[:2000],
            "TaskID":        task_id,
            "ScriptPath":    script_path,
            "Status":        status,
            "Duration_s":    round(result.duration_seconds, 2),
            "Stdout":        result.stdout[:2000],
            "Stderr":        result.stderr[:500],
            "PlanPath":      str(plan_path),
            "LogPath":       result.run_log_path,
            "ResultSummary": summary_md[:5000],
        })
        logger.info("Bitable record written: %s", record_id)

        # Send experiment card
        plan_summary = plan_text[:_PLAN_CARD_MAX] if plan_text else "(计划文件不存在)"
        await svc.messaging.send_experiment_card(
            receive_id=chat_id,
            receive_id_type="chat_id",
            task_id=task_id,
            command=user_text[:200],
            plan_summary=plan_summary,
            result_summary=summary_md,
            status=status,
            duration=result.duration_seconds,
        )

    except asyncio.TimeoutError:
        logger.error("Task %s timed out", task_id)
        await notify("⏰ 实验脚本执行超时，任务已终止。")
    except Exception as exc:
        logger.exception("Background task %s failed: %s", task_id, exc)
        await notify(f"❌ 发生错误：{exc}")
    finally:
        # Clean up dedup set to avoid unbounded growth in long-running servers
        if event.message_id:
            svc.processing_ids.discard(event.message_id)
