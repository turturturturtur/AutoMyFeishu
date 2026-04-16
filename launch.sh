cd /home/vepfs/AutoMyFeishu
uvicorn "claude_feishu_flow.server.app:create_app_from_env" --factory --host 0.0.0.0 --port 9090
