from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Config(BaseSettings):
    # Feishu App credentials
    feishu_app_id: str
    feishu_app_secret: str
    feishu_verification_token: str
    feishu_encrypt_key: str = ""

    # Feishu Bitable target
    bitable_app_token: str
    # table_id is discovered/created automatically via ensure_experiment_table()
    # Optionally set to skip the lookup on every startup (populated at runtime).
    bitable_table_id: str = ""

    # Claude / Anthropic
    anthropic_api_key: str
    # Model name — use any Claude model or a compatible third-party model name.
    anthropic_model: str = "claude-3-5-sonnet-latest"
    # Optional: override the Anthropic API base URL for mirror / proxy endpoints.
    # e.g. "https://your-proxy.example.com/v1"
    # Leave unset (or empty) to use the official Anthropic API.
    anthropic_base_url: Optional[str] = None

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Experiments directory (each experiment gets its own subdirectory exp_<uuid>)
    experiments_dir: str = "./Experiments"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def resolved_experiments_dir(self) -> Path:
        """Return the experiments directory as an absolute Path, creating it if needed."""
        path = Path(self.experiments_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

