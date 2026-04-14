from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings


class Config(BaseSettings):
    # Feishu App credentials
    feishu_app_id: str
    feishu_app_secret: str
    feishu_verification_token: str
    feishu_encrypt_key: str = ""

    # Feishu Bitable target
    bitable_app_token: str
    bitable_table_id: str

    # Claude / Anthropic
    anthropic_api_key: str
    claude_model: str = "claude-opus-4-6"

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Workspace directory (each task gets its own subdirectory task_<uuid>)
    workspaces_dir: str = "./workspaces"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def resolved_workspaces_dir(self) -> Path:
        """Return the workspaces directory as an absolute Path, creating it if needed."""
        path = Path(self.workspaces_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
