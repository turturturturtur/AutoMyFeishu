"""FastAPI application factory, lifespan, and shared services container."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import FastAPI

from claude_feishu_flow.ai.client import ClaudeClient
from claude_feishu_flow.ai.kimi_client import KimiClient
from claude_feishu_flow.config import Config
from claude_feishu_flow.feishu.auth import TokenManager
from claude_feishu_flow.feishu.bitable import BitableClient
from claude_feishu_flow.feishu.client import FeishuClient
from claude_feishu_flow.feishu.messaging import Messaging
from claude_feishu_flow.runner.executor import ScriptExecutor

logger = logging.getLogger(__name__)


@dataclass
class EditSession:
    """State for an ongoing /edit interactive conversation.

    The background coroutine blocks on queue.get() between user turns.
    The webhook handler pushes incoming messages into the queue.
    task_id and exp_dir are set at session creation time.
    """
    task_id: str
    exp_dir_str: str                              # str so it's easily serialisable
    queue: asyncio.Queue                          # str messages from user
    max_retries: int = 0
    done: bool = False                            # set True when session ends

    @property
    def exp_dir(self):  # type: ignore[return]
        from pathlib import Path
        return Path(self.exp_dir_str)


@dataclass
class Services:
    """All shared singleton objects, injected into route handlers via app.state."""

    config: Config
    http: httpx.AsyncClient
    token_manager: TokenManager
    feishu: FeishuClient
    messaging: Messaging
    bitable: BitableClient
    ai: Any  # ClaudeClient or KimiClient
    executor: ScriptExecutor
    # In-memory dedup set: prevents re-processing Feishu retry events
    processing_ids: set[str] = field(default_factory=set)
    # Active /edit sessions keyed by chat_id
    edit_sessions: dict[str, EditSession] = field(default_factory=dict)
    # Multi-agent session routing: maps open_id → "main" (default) or "exp_<uuid>"
    user_sessions: dict[str, str] = field(default_factory=dict)
    # Sub Agent conversation histories: maps task_id → list of message dicts
    sub_agent_histories: dict[str, list[dict]] = field(default_factory=dict)
    # Per-task locks to prevent concurrent Sub Agent turns corrupting history
    sub_agent_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Maps card message_id → task_id for parent_id-based routing (引用回复实验卡片)
    msg_to_task: dict[str, str] = field(default_factory=dict)


def create_app(config: Config) -> FastAPI:
    """Factory function: wire all dependencies and return a configured FastAPI app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # ── STARTUP ──────────────────────────────────────────────────────────
        logger.info("Starting up claude-feishu-flow...")

        http = httpx.AsyncClient(timeout=30.0)
        token_manager = TokenManager(http, config.feishu_app_id, config.feishu_app_secret)
        await token_manager.start()

        feishu_client = FeishuClient(token_manager, http)
        messaging = Messaging(feishu_client)
        bitable = BitableClient(feishu_client, config.bitable_app_token, config.bitable_table_id)

        # Auto-create/find the Experiment_Results table
        await bitable.ensure_experiment_table()

        if config.llm_provider == "kimi":
            if not config.kimi_api_key:
                raise ValueError("配置错误: llm_provider='kimi' 但未提供 KIMI_API_KEY")
            ai_client: Any = KimiClient(api_key=config.kimi_api_key, model=config.kimi_model, base_url=config.kimi_base_url)
        else:
            if not config.anthropic_api_key:
                raise ValueError("配置错误: llm_provider='anthropic' 但未提供 ANTHROPIC_API_KEY")
            ai_client = ClaudeClient(
                api_key=config.anthropic_api_key,
                model=config.anthropic_model,
                base_url=config.anthropic_base_url or None,
            )
        executor = ScriptExecutor()

        app.state.services = Services(
            config=config,
            http=http,
            token_manager=token_manager,
            feishu=feishu_client,
            messaging=messaging,
            bitable=bitable,
            ai=ai_client,
            executor=executor,
        )

        logger.info("Startup complete.")
        yield

        # ── SHUTDOWN ─────────────────────────────────────────────────────────
        logger.info("Shutting down...")
        await token_manager.stop()
        await http.aclose()
        logger.info("Shutdown complete.")

    app = FastAPI(title="claude-feishu-flow", lifespan=lifespan)

    from claude_feishu_flow.server.routes import router
    app.include_router(router)

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn-compatible factory: loads Config from environment/.env, then calls create_app."""
    logging.basicConfig(level=logging.INFO)
    config = Config()
    return create_app(config)
