"""FastAPI webhook route and background pipeline handler."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from claude_feishu_flow.feishu.webhook import parse_webhook_event, verify_feishu_signature_v2, decrypt_feishu_message
from claude_feishu_flow.ai.client import SubAgentResult

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

    # ── Card action events (interactive card button clicks) ──────────────
    if event.event_type == "card.action.trigger":
        # Flat card callbacks include a token field for verification
        card_token = raw.get("token", "")
        if card_token and card_token != svc.config.feishu_verification_token:
            logger.warning("Card callback token mismatch")
            return JSONResponse({"code": 40001, "msg": "invalid token"}, status_code=401)

        action_value: dict = event.action_value
        action_key: str = action_value.get("key", "")

        if action_key == "enter_session":
            task_id_val: str = action_value.get("task_id", "")
            open_id_val: str = event.open_id or ""
            chat_id_val: str = event.action_chat_id or ""
            background_tasks.add_task(
                _handle_enter_session,
                open_id=open_id_val,
                chat_id=chat_id_val,
                task_id=task_id_val,
                svc=svc,
            )

        return JSONResponse({
            "toast": {
                "type": "info",
                "content": "请求已接收，正在进入会话...",
            }
        })

    # ── Only handle message events ────────────────────────────────────────
    if event.event_type != "im.message.receive_v1":
        return JSONResponse({"code": 0, "msg": "ignored"})

    has_content = (event.text and event.text.strip()) or event.image_keys
    if not has_content:
        return JSONResponse({"code": 0, "msg": "empty message ignored"})

    chat_id: str = event.chat_id or ""
    user_text: str = event.text.strip() if event.text else ""
    open_id: str = event.open_id or ""

    # ── Deduplication: Feishu retries on timeout, avoid double-processing ─
    if event.message_id and event.message_id in svc.processing_ids:
        logger.info("Duplicate message_id=%s, skipping", event.message_id)
        return JSONResponse({"code": 0, "msg": "duplicate"})

    if event.message_id:
        svc.processing_ids.add(event.message_id)

    # ── /exit command: reset Sub Agent session regardless of current state ─
    if user_text == "/exit":
        old_session = svc.user_sessions.pop(open_id, None)
        if old_session:
            await svc.messaging.send_text(
                chat_id,
                f"已退出实验 `{old_session}` 的会话，回到主界面。",
            )
        else:
            await svc.messaging.send_text(chat_id, "当前没有活跃的实验会话。")
        if event.message_id:
            svc.processing_ids.discard(event.message_id)
        return JSONResponse({"code": 0, "msg": "exit_handled"})

    # ── Sub Agent routing: if user has active session, forward there ──────
    current_session = svc.user_sessions.get(open_id, "main")
    if current_session != "main":
        background_tasks.add_task(
            _handle_sub_agent_message,
            open_id=open_id,
            chat_id=chat_id,
            task_id=current_session,
            user_text=user_text,
            svc=svc,
            event_message_id=event.message_id,
        )
        return JSONResponse({"code": 0, "msg": "routed_to_sub_agent"})

    # ── If an edit session is active for this chat, route message into it ─
    if chat_id and chat_id in svc.edit_sessions:
        session = svc.edit_sessions[chat_id]
        if not session.done:
            await session.queue.put(user_text)
            return JSONResponse({"code": 0, "msg": "routed_to_session"})
        else:
            # Session ended; clean up and fall through to normal handling
            del svc.edit_sessions[chat_id]

    # ── Register background task and IMMEDIATELY return 200 ───────────────
    background_tasks.add_task(_handle_message, event=event, svc=svc)
    return JSONResponse({"code": 0, "msg": "accepted"})


# ---------------------------------------------------------------------------
# Background pipeline — runs after HTTP 200 has been sent to Feishu
# ---------------------------------------------------------------------------

async def _handle_message(event, svc) -> None:  # type: ignore[no-untyped-def]
    """Full pipeline:

    Argument parsing:
      --retry N  — retry up to N times on execution failure (self-healing).
      /list      — list all experiments.
      /edit exp_<uuid> <instruction>  — start interactive edit session.
      /cancel    — cancel active edit session.

    Phase A (Generation) — Claude generates plan.md and main.py.
    Phase B (Execution)  — Python runs setting/main.py, with optional self-healing loop.
    Phase C (Reporting)  — Results written to Bitable, user notified via card.
    """
    chat_id: str = event.chat_id or ""
    user_text: str = event.text or ""

    # Detect /edit prefix — must validate strictly before falling through
    task_id: str
    exp_dir: Path

    if user_text.strip() == "/list":
        await _handle_list(chat_id, svc)
        return

    if user_text.strip() == "/cancel":
        if chat_id in svc.edit_sessions:
            svc.edit_sessions[chat_id].queue.put_nowait(None)  # sentinel to stop loop
        await svc.messaging.send_text(chat_id, "✅ 编辑会话已取消。")
        return

    if user_text.startswith("/edit"):
        edit_match = re.match(r'^/edit\s+(exp_[0-9a-f-]+)\s+(\S.*)', user_text, re.DOTALL)
        if not edit_match:
            # Malformed /edit — show help card and stop
            await svc.messaging.send_help_card(
                receive_id=chat_id,
                receive_id_type="chat_id",
                error_msg=(
                    f"无法解析 `/edit` 指令：`{user_text[:200]}`\n\n"
                    "正确格式：`/edit exp_<uuid> <修改指令>`"
                ),
            )
            return
        task_id = edit_match.group(1)
        instruction = edit_match.group(2).strip()
        exp_dir = svc.config.resolved_experiments_dir() / task_id
        if not (exp_dir / "setting" / "main.py").exists():
            await svc.messaging.send_help_card(
                receive_id=chat_id,
                receive_id_type="chat_id",
                error_msg=f"实验 `{task_id}` 不存在或尚未生成脚本，无法编辑。",
            )
            return

        # Extract --retry N from the instruction
        retry_match = re.search(r'\s*--retry\s+(\d+)', instruction)
        max_retries: int = int(retry_match.group(1)) if retry_match else 0
        if retry_match:
            instruction = (instruction[:retry_match.start()] + instruction[retry_match.end():]).strip()

        from claude_feishu_flow.server.app import EditSession
        session = EditSession(
            task_id=task_id,
            exp_dir_str=str(exp_dir),
            queue=asyncio.Queue(),
            max_retries=max_retries,
        )
        svc.edit_sessions[chat_id] = session

        asyncio.create_task(
            _handle_edit_session(
                chat_id=chat_id,
                session=session,
                initial_instruction=instruction,
                svc=svc,
                event_message_id=event.message_id,
            )
        )
        return

    # ── Extract --retry N (only meaningful for /launch) ───────────────────
    retry_match = re.search(r'\s*--retry\s+(\d+)', user_text)
    max_retries: int = int(retry_match.group(1)) if retry_match else 0
    if retry_match:
        user_text = (user_text[:retry_match.start()] + user_text[retry_match.end():]).strip()

    if user_text.startswith("/launch"):
        # ── /launch: strip prefix and run the full experiment pipeline ───
        launch_text = user_text[len("/launch"):].strip()
        if not launch_text:
            await svc.messaging.send_text(
                chat_id,
                "请在 /launch 后附上实验指令，例如：/launch 训练一个线性回归模型",
            )
            if event.message_id:
                svc.processing_ids.discard(event.message_id)
            return
        user_text = launch_text

        task_id = f"exp_{uuid.uuid4()}"
        exp_dir = svc.config.resolved_experiments_dir() / task_id
        for sub in ("setting", "output", "results"):
            (exp_dir / sub).mkdir(parents=True, exist_ok=True)

        logger.info(
            "Background task started task_id=%s chat_id=%s max_retries=%d exp_dir=%s",
            task_id, chat_id, max_retries, exp_dir,
        )

        async def notify(text: str) -> None:
            try:
                await svc.messaging.send_text(chat_id, text)
            except Exception as exc:
                logger.warning("Failed to send notification: %s", exc)

        loading_msg_id = await svc.messaging.send_text(chat_id, "⏳ 正在处理中，请稍候...")
        try:
            # ── Phase A: Generation ───────────────────────────────────────────
            await notify("正在理解需求，生成实验计划和脚本，请稍候...")

            # ── Image download & archive ──────────────────────────────────────
            images: list[dict] = []
            if event.image_keys and event.message_id:
                setting_dir = exp_dir / "setting"
                for idx, img_key in enumerate(event.image_keys, start=1):
                    try:
                        img_bytes = await svc.feishu.download_resource(
                            message_id=event.message_id,
                            file_key=img_key,
                        )
                        save_path = setting_dir / f"input_image_{idx}.jpg"
                        save_path.write_bytes(img_bytes)
                        b64 = base64.b64encode(img_bytes).decode("utf-8")
                        images.append({"media_type": "image/jpeg", "base64_data": b64})
                        logger.info("Downloaded and archived image %s → %s", img_key, save_path)
                    except Exception as exc:
                        logger.warning("Failed to download image %s: %s", img_key, exc)

            script_path = await svc.ai.generate_experiment(
                user_text=user_text or "(用户发送了图片，请参考图片内容生成实验脚本)",
                workspace_dir=exp_dir,
                images=images if images else None,
            )
            logger.info("Phase A complete: script_path=%s", script_path)

            await _run_phase_b_and_c(
                chat_id=chat_id,
                task_id=task_id,
                exp_dir=exp_dir,
                command_text=user_text,
                max_retries=max_retries,
                svc=svc,
                notify=notify,
            )

        except asyncio.TimeoutError:
            logger.error("Task %s timed out", task_id)
            await notify("⏰ 实验脚本执行超时，任务已终止。")
        except Exception as exc:
            logger.exception("Background task %s failed: %s", task_id, exc)
            await notify(f"❌ 发生错误：{exc}")
        finally:
            await svc.messaging.delete_message(loading_msg_id)
            if event.message_id:
                svc.processing_ids.discard(event.message_id)

    else:
        # ── Default: casual chat ──────────────────────────────────────────
        images: list[dict] = []
        if event.image_keys and event.message_id:
            for idx, img_key in enumerate(event.image_keys, start=1):
                try:
                    img_bytes = await svc.feishu.download_resource(
                        message_id=event.message_id,
                        file_key=img_key,
                    )
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    images.append({"media_type": "image/jpeg", "base64_data": b64})
                    logger.info("Downloaded image for casual chat: %s", img_key)
                except Exception as exc:
                    logger.warning("Failed to download image %s: %s", img_key, exc)

        try:
            reply_text = await svc.ai.chat_casual(
                user_text=user_text or "(用户发送了图片)",
                images=images if images else None,
            )
            await svc.messaging.send_markdown(chat_id, reply_text)
        except Exception as exc:
            logger.exception("Casual chat failed: %s", exc)
            await svc.messaging.send_text(chat_id, f"❌ 发生错误：{exc}")
        finally:
            if event.message_id:
                svc.processing_ids.discard(event.message_id)


async def _handle_list(chat_id: str, svc) -> None:  # type: ignore[no-untyped-def]
    """List all experiment directories and send a summary card."""
    experiments_dir = svc.config.resolved_experiments_dir()
    entries = sorted(
        (d for d in experiments_dir.iterdir() if d.is_dir() and d.name.startswith("exp_")),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    ) if experiments_dir.exists() else []

    await svc.messaging.send_list_card(
        receive_id=chat_id,
        receive_id_type="chat_id",
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Card action: enter Sub Agent session
# ---------------------------------------------------------------------------

async def _handle_enter_session(
    open_id: str,
    chat_id: str,
    task_id: str,
    svc,  # type: ignore[no-untyped-def]
) -> None:
    """Handle card button click to enter a Sub Agent session."""
    exp_dir = svc.config.resolved_experiments_dir() / task_id

    if not exp_dir.exists():
        await svc.messaging.send_text(
            chat_id,
            f"实验 `{task_id}` 不存在，无法进入会话。",
        )
        return

    svc.user_sessions[open_id] = task_id
    await svc.messaging.send_text(
        chat_id,
        f"已进入实验 **{task_id}** 的对话模式。\n"
        "你可以直接提问（如'当前 loss 多少？'），Sub Agent 会读取实时日志回答。\n"
        "发送 `/exit` 退出当前实验会话。",
    )


# ---------------------------------------------------------------------------
# Sub Agent message handler
# ---------------------------------------------------------------------------

async def _handle_sub_agent_message(
    open_id: str,
    chat_id: str,
    task_id: str,
    user_text: str,
    svc,  # type: ignore[no-untyped-def]
    event_message_id: str | None = None,
) -> None:
    """Forward a user message to the Sub Agent for a specific experiment."""
    exp_dir = svc.config.resolved_experiments_dir() / task_id

    if not exp_dir.exists():
        svc.user_sessions.pop(open_id, None)
        svc.sub_agent_histories.pop(task_id, None)
        await svc.messaging.send_text(
            chat_id,
            f"实验 `{task_id}` 已不存在，自动退出会话。",
        )
        if event_message_id:
            svc.processing_ids.discard(event_message_id)
        return

    # Get or create conversation history and per-task lock for this experiment.
    # The lock serialises concurrent turns so history is never mutated by two
    # coroutines at the same time (Feishu can deliver rapid messages in parallel).
    if task_id not in svc.sub_agent_histories:
        svc.sub_agent_histories[task_id] = []
    if task_id not in svc.sub_agent_locks:
        svc.sub_agent_locks[task_id] = asyncio.Lock()
    history = svc.sub_agent_histories[task_id]
    lock = svc.sub_agent_locks[task_id]

    loading_msg_id = await svc.messaging.send_text(chat_id, "⏳ 正在处理中，请稍候...")
    try:
        async with lock:
            result: SubAgentResult = await svc.ai.chat_with_sub_agent(
                task_id=task_id,
                user_text=user_text,
                exp_dir=exp_dir,
                history=history,
            )
        await svc.messaging.send_markdown(chat_id, result.text)
        if result.needs_restart:
            asyncio.create_task(
                _restart_and_notify(
                    svc=svc,
                    task_id=task_id,
                    exp_dir=exp_dir,
                    chat_id=chat_id,
                )
            )
    except Exception as exc:
        logger.exception("Sub agent error for task=%s: %s", task_id, exc)
        await svc.messaging.send_text(chat_id, f"Sub Agent 出错：{exc}")
    finally:
        await svc.messaging.delete_message(loading_msg_id)
        if event_message_id:
            svc.processing_ids.discard(event_message_id)


# ---------------------------------------------------------------------------
# Sub Agent restart helper
# ---------------------------------------------------------------------------

async def _restart_and_notify(
    svc,  # type: ignore[no-untyped-def]
    task_id: str,
    exp_dir: Path,
    chat_id: str,
) -> None:
    """Kill any running process for task_id, start a fresh one, then report."""
    await svc.messaging.send_text(chat_id, "🚀 Sub Agent 已为您更新代码并重启实验！")
    try:
        result = await svc.executor.run(exp_dir, task_id)
        if result.was_killed:
            # This run was itself superseded by yet another restart; stay silent
            return
        status = "success" if result.returncode == 0 else "failed"
        plan_path = exp_dir / "setting" / "plan.md"
        plan_text = _tail_file(plan_path, 99999)
        run_log = _tail_file(exp_dir / "output" / "run.log", _LOG_TAIL_RUN)
        err_log = _tail_file(exp_dir / "output" / "error.log", _LOG_TAIL_ERR)
        log_text = ""
        if run_log:
            log_text += f"### stdout\n{run_log}\n\n"
        if err_log:
            log_text += f"### stderr\n{err_log}\n"
        if not log_text:
            log_text = "(无日志输出)"
        summary_md = await svc.ai.summarize_experiment(plan_text, log_text)
        await svc.messaging.send_experiment_card(
            receive_id=chat_id,
            receive_id_type="chat_id",
            task_id=task_id,
            command="[Sub Agent 重启]",
            plan_summary=plan_text[:_PLAN_CARD_MAX] if plan_text else "(无计划文件)",
            result_summary=summary_md,
            status=status,
            duration=result.duration_seconds,
            repair_count=0,
        )
    except asyncio.TimeoutError:
        await svc.messaging.send_text(chat_id, "⏰ 重启后的实验执行超时，任务已终止。")
    except Exception as exc:
        logger.exception("_restart_and_notify failed for task=%s: %s", task_id, exc)
        await svc.messaging.send_text(chat_id, f"❌ 重启实验时出错：{exc}")


# ---------------------------------------------------------------------------
# Shared Phase B + C helper (used by both normal and edit pipelines)
# ---------------------------------------------------------------------------

async def _run_phase_b_and_c(
    chat_id: str,
    task_id: str,
    exp_dir: Path,
    command_text: str,
    max_retries: int,
    svc,  # type: ignore[no-untyped-def]
    notify,  # async callable(str)
) -> None:
    """Execute setting/main.py with optional self-healing, then run AI analysis
    and send the final experiment card.  Raises on unrecoverable errors."""

    await notify(
        f"实验脚本已生成，正在后台执行...\n"
        f"实验 ID：{task_id}\n"
        f"计划文件：{exp_dir / 'setting' / 'plan.md'}"
    )

    repair_count = 0
    for attempt in range(max_retries + 1):
        result = await svc.executor.run(exp_dir, task_id)
        if result.was_killed:
            # A Sub Agent restart superseded this run; silently exit
            logger.info("Phase B for task=%s was superseded by a restart, exiting silently", task_id)
            return
        if result.returncode == 0:
            break
        if attempt < max_retries:
            repair_count += 1
            await notify(
                f"⚠️ 实验报错，正在进行第 {repair_count}/{max_retries} 次自动修复..."
            )
            await svc.ai.fix_experiment(exp_dir, result.stderr)

    status = "success" if result.returncode == 0 else "failed"
    logger.info(
        "Phase B complete: task=%s status=%s returncode=%d duration=%.2fs repair_count=%d",
        task_id, status, result.returncode, result.duration_seconds, repair_count,
    )

    # ── Phase C: AI Analysis + Card ───────────────────────────────────────
    await notify("正在用 AI 分析实验结果，请稍候...")

    plan_path = exp_dir / "setting" / "plan.md"
    plan_text = _tail_file(plan_path, 99999)

    run_log = _tail_file(exp_dir / "output" / "run.log", _LOG_TAIL_RUN)
    err_log = _tail_file(exp_dir / "output" / "error.log", _LOG_TAIL_ERR)
    log_text = ""
    if run_log:
        log_text += f"### stdout (run.log)\n{run_log}\n\n"
    if err_log:
        log_text += f"### stderr (error.log)\n{err_log}\n"
    if not log_text:
        log_text = "(无日志输出)"

    summary_md = await svc.ai.summarize_experiment(plan_text, log_text)
    logger.info("Phase C: AI summary generated (%d chars)", len(summary_md))

    summary_path = exp_dir / "results" / "summary.md"
    summary_path.write_text(summary_md, encoding="utf-8")

    script_path = str(exp_dir / "setting" / "main.py")
    await svc.bitable.append_record({
        "Command":       command_text[:2000],
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

    plan_summary = plan_text[:_PLAN_CARD_MAX] if plan_text else "(计划文件不存在)"
    await svc.messaging.send_experiment_card(
        receive_id=chat_id,
        receive_id_type="chat_id",
        task_id=task_id,
        command=command_text[:200],
        plan_summary=plan_summary,
        result_summary=summary_md,
        status=status,
        duration=result.duration_seconds,
        repair_count=repair_count,
    )


# ---------------------------------------------------------------------------
# Interactive edit session pipeline
# ---------------------------------------------------------------------------

async def _handle_edit_session(
    chat_id: str,
    session,  # EditSession
    initial_instruction: str,
    svc,  # type: ignore[no-untyped-def]
    event_message_id: str,
) -> None:
    """Drive a multi-turn /edit conversation then execute and report results."""

    async def notify(text: str) -> None:
        try:
            await svc.messaging.send_text(chat_id, text)
        except Exception as exc:
            logger.warning("Failed to send notification: %s", exc)

    async def reply(text: str) -> None:
        try:
            await svc.messaging.send_markdown(chat_id, text)
        except Exception as exc:
            logger.warning("Failed to send markdown reply: %s", exc)

    exp_dir = session.exp_dir
    task_id = session.task_id

    loading_msg_id = await svc.messaging.send_text(chat_id, "⏳ 正在处理中，请稍候...")
    try:
        await notify(
            f"🗣️ 已进入编辑对话模式，实验 ID：{task_id}\n"
            "你可以直接和我对话，告诉我想要的修改。发送 /cancel 可随时退出。"
        )

        ready = await svc.ai.chat_edit(
            exp_dir=exp_dir,
            initial_instruction=initial_instruction,
            user_queue=session.queue,
            reply_callback=reply,
        )

        if not ready:
            await notify("📝 编辑会话已结束，未执行脚本。")
            return

        # Ensure output/ and results/ dirs exist after potential fresh edit
        for sub in ("output", "results"):
            (exp_dir / sub).mkdir(parents=True, exist_ok=True)

        await _run_phase_b_and_c(
            chat_id=chat_id,
            task_id=task_id,
            exp_dir=exp_dir,
            command_text=initial_instruction,
            max_retries=session.max_retries,
            svc=svc,
            notify=notify,
        )

    except asyncio.TimeoutError:
        logger.error("Edit session %s timed out", task_id)
        await notify("⏰ 实验脚本执行超时，任务已终止。")
    except Exception as exc:
        logger.exception("Edit session %s failed: %s", task_id, exc)
        await notify(f"❌ 发生错误：{exc}")
    finally:
        await svc.messaging.delete_message(loading_msg_id)
        session.done = True
        svc.edit_sessions.pop(chat_id, None)
        if event_message_id:
            svc.processing_ids.discard(event_message_id)
