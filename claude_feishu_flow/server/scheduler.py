"""APScheduler-based cron job manager for the MLOps Orchestrator.

Provides SchedulerManager, a thin wrapper around AsyncIOScheduler that:
  - Starts/stops with the FastAPI lifespan context.
  - Exposes add_cron_job() for the create_cron_job tool.
  - Fires cron callbacks that proactively call chat_main_agent and push
    the result to the originating Feishu chat.
  - Persists jobs to logs/cron_jobs.json so they survive server restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from claude_feishu_flow.server.app import Services

logger = logging.getLogger(__name__)

_CRON_JSON = Path("logs/cron_jobs.json")


class SchedulerManager:
    """Wraps APScheduler's AsyncIOScheduler and integrates with Services."""

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler()
        self._services: Services | None = None
        # Metadata keyed by job_id: {cron_expression, task_description, chat_id}
        self._job_meta: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_services(self, services: Services) -> None:
        """Inject the Services singleton after it has been constructed."""
        self._services = services

    def start(self) -> None:
        """Start the scheduler. Call from FastAPI lifespan startup."""
        self._scheduler.start()
        logger.info("APScheduler started")
        self._load_jobs()

    def shutdown(self) -> None:
        """Shut down the scheduler. Call from FastAPI lifespan shutdown."""
        self._scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_jobs(self) -> None:
        """Overwrite _CRON_JSON with current _job_meta."""
        try:
            _CRON_JSON.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {"job_id": jid, **meta}
                for jid, meta in self._job_meta.items()
            ]
            _CRON_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Failed to persist cron jobs to %s", _CRON_JSON)

    def _load_jobs(self) -> None:
        """Read _CRON_JSON and restore all cron jobs into the scheduler."""
        if not _CRON_JSON.exists():
            return
        try:
            records: list[dict[str, str]] = json.loads(_CRON_JSON.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read cron jobs from %s; skipping restore", _CRON_JSON)
            return
        for rec in records:
            job_id = rec.get("job_id", "")
            cron_expr = rec.get("cron_expression", "")
            task_desc = rec.get("task_description", "")
            chat_id = rec.get("chat_id", "")
            if not (job_id and cron_expr and chat_id):
                logger.warning("Skipping malformed cron record: %r", rec)
                continue
            try:
                self.add_cron_job(cron_expr, task_desc, chat_id, restore_id=job_id)
            except Exception:
                logger.exception("Failed to restore cron job %r", job_id)
        logger.info("Restored %d cron job(s) from %s", len(records), _CRON_JSON)

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def add_cron_job(
        self,
        cron_expr: str,
        task_description: str,
        chat_id: str,
        restore_id: str | None = None,
    ) -> str:
        """Register a recurring cron job and return its job_id.

        Args:
            cron_expr:        Standard 5-field cron expression (local time).
                              Example: "0 9 * * *" = daily at 9am.
            task_description: Human-readable description for the trigger prompt.
            chat_id:          Feishu chat_id to send the proactive message to.
            restore_id:       When restoring from JSON, pass the saved job_id so
                              APScheduler uses the same ID. Leave None for new jobs.

        Returns:
            APScheduler job ID string.

        Raises:
            ValueError: If cron_expr is not a valid 5-field expression.
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Invalid cron expression {cron_expr!r}. "
                "Expected 5 fields: minute hour day month weekday."
            )
        minute, hour, day, month, dow = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
        )
        add_kwargs: dict[str, Any] = dict(
            misfire_grace_time=300,
            coalesce=True,
        )
        if restore_id:
            add_kwargs["id"] = restore_id

        job = self._scheduler.add_job(
            self._fire,
            trigger,
            kwargs={"task_description": task_description, "chat_id": chat_id},
            **add_kwargs,
        )
        self._job_meta[job.id] = {
            "cron_expression": cron_expr,
            "task_description": task_description,
            "chat_id": chat_id,
        }
        logger.info(
            "Cron job registered: id=%s cron=%r task=%r chat=%s",
            job.id, cron_expr, task_description, chat_id,
        )
        if not restore_id:
            self._save_jobs()
        return job.id

    def list_jobs(self) -> str:
        """Return a human-readable summary of all scheduled jobs.

        Called by the list_cron_jobs tool handler in chat_main_agent.
        """
        jobs = self._scheduler.get_jobs()
        if not jobs:
            return "当前无运行中的定时任务。"
        lines: list[str] = []
        for job in jobs:
            trigger_str = str(job.trigger)
            kwargs = job.kwargs or {}
            desc = kwargs.get("task_description", "(无描述)")
            lines.append(f"- ID: {job.id}\n  触发规则: {trigger_str}\n  描述: {desc}")
        return "\n".join(lines)

    def get_jobs_for_api(self) -> list[dict[str, str]]:
        """Return structured job list for the REST API."""
        result: list[dict[str, str]] = []
        for job in self._scheduler.get_jobs():
            meta = self._job_meta.get(job.id, {})
            next_run = job.next_run_time
            next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else "—"
            result.append({
                "job_id": job.id,
                "cron_expression": meta.get("cron_expression", ""),
                "task_description": meta.get("task_description", ""),
                "chat_id": meta.get("chat_id", ""),
                "next_run_time": next_run_str,
            })
        return result

    def cancel_job(self, job_id: str) -> str:
        """Remove a scheduled job by ID.

        Called by the cancel_cron_job tool handler in chat_main_agent.

        Returns a human-readable success/failure message.
        """
        try:
            self._scheduler.remove_job(job_id)
            self._job_meta.pop(job_id, None)
            self._save_jobs()
            logger.info("Cron job cancelled: id=%s", job_id)
            return f"✅ 定时任务 {job_id} 已成功取消。"
        except Exception:
            logger.warning("cancel_job: job_id=%s not found", job_id)
            return f"❌ 找不到 ID 为 {job_id!r} 的定时任务，可能已被取消或从未存在。"

    # ------------------------------------------------------------------
    # Internal callback
    # ------------------------------------------------------------------

    async def _fire(self, task_description: str, chat_id: str) -> None:
        """Callback executed at each cron fire time.

        Calls chat_main_agent with a trigger prompt instructing it to
        summarize experiments and notify the user.
        """
        svc = self._services
        if svc is None:
            logger.warning("Cron _fire() called but Services not set; skipping.")
            return
        try:
            trigger_text = (
                f"[定时任务触发] {task_description}。"
                "请调用 list_experiments 汇总当前所有实验进度，并将结果发送给用户。"
            )
            history = svc.main_agent_histories.setdefault(chat_id, [])
            result = await svc.ai.chat_main_agent(
                user_text=trigger_text,
                exp_base_dir=svc.config.resolved_experiments_dir(),
                history=history,
                scheduler=self,
            )
            if result.text:
                await svc.messaging.send_markdown(chat_id, result.text)
            if result.plot_path:
                try:
                    image_bytes = Path(result.plot_path).read_bytes()
                    image_key = await svc.feishu.upload_image(image_bytes)
                    await svc.messaging.send_image(chat_id, image_key)
                except Exception as img_exc:
                    logger.warning("Cron _fire(): failed to send plot: %s", img_exc)
        except Exception:
            logger.exception(
                "Cron job _fire() failed: task=%r chat_id=%s", task_description, chat_id
            )
