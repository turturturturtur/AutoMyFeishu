"""APScheduler-based cron job manager for the MLOps Orchestrator.

Provides SchedulerManager, a thin wrapper around AsyncIOScheduler that:
  - Starts/stops with the FastAPI lifespan context.
  - Exposes add_cron_job() for the create_cron_job tool.
  - Fires cron callbacks that proactively call chat_main_agent and push
    the result to the originating Feishu chat.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from claude_feishu_flow.server.app import Services

logger = logging.getLogger(__name__)


class SchedulerManager:
    """Wraps APScheduler's AsyncIOScheduler and integrates with Services."""

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler()
        self._services: Services | None = None

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

    def shutdown(self) -> None:
        """Shut down the scheduler. Call from FastAPI lifespan shutdown."""
        self._scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def add_cron_job(
        self,
        cron_expr: str,
        task_description: str,
        chat_id: str,
    ) -> str:
        """Register a recurring cron job and return its job_id.

        Args:
            cron_expr:        Standard 5-field cron expression (local time).
                              Example: "0 9 * * *" = daily at 9am.
            task_description: Human-readable description for the trigger prompt.
            chat_id:          Feishu chat_id to send the proactive message to.

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
        job = self._scheduler.add_job(
            self._fire,
            trigger,
            kwargs={"task_description": task_description, "chat_id": chat_id},
            misfire_grace_time=300,  # fire within 5 minutes of scheduled time
            coalesce=True,
        )
        logger.info(
            "Cron job registered: id=%s cron=%r task=%r chat=%s",
            job.id, cron_expr, task_description, chat_id,
        )
        return job.id

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
            )
            if result.text:
                await svc.messaging.send_markdown(chat_id, result.text)
            if result.plot_path:
                try:
                    from pathlib import Path
                    image_bytes = Path(result.plot_path).read_bytes()
                    image_key = await svc.feishu.upload_image(image_bytes)
                    await svc.messaging.send_image(chat_id, image_key)
                except Exception as img_exc:
                    logger.warning("Cron _fire(): failed to send plot: %s", img_exc)
        except Exception:
            logger.exception(
                "Cron job _fire() failed: task=%r chat_id=%s", task_description, chat_id
            )
