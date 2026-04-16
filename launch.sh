#!/usr/bin/env bash
cd "$(dirname "$0")"
exec uvicorn "claude_feishu_flow.server.app:create_app_from_env" \
  --factory --host 0.0.0.0 --port 8080
