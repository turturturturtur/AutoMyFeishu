"""Manual integration test for Step 2/3/4 — requires a real .env file.

Run:
    python scripts/test_feishu_api.py

What it tests:
    1. TokenManager: fetch and cache tenant_access_token from Feishu
    2. FeishuClient: make an authenticated GET to verify the token works
    3. Messaging.send_text: send a text message to a target chat/user

Set TARGET_RECEIVE_ID and TARGET_RECEIVE_ID_TYPE below (or via env vars).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Add project root to path so we can import without installing
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# -----------------------------------------------------------------------
# Configuration — edit these or set as environment variables
# -----------------------------------------------------------------------
TARGET_RECEIVE_ID = os.getenv("TEST_RECEIVE_ID", "")     # e.g. your chat_id or open_id
TARGET_RECEIVE_ID_TYPE = os.getenv("TEST_RECEIVE_ID_TYPE", "chat_id")  # chat_id | open_id
# -----------------------------------------------------------------------


async def main() -> None:
    import httpx
    from claude_feishu_flow.config import Config
    from claude_feishu_flow.feishu.auth import TokenManager
    from claude_feishu_flow.feishu.client import FeishuClient
    from claude_feishu_flow.feishu.messaging import Messaging

    # Load config from .env
    try:
        config = Config()
    except Exception as e:
        print(f"[FAIL] Could not load Config from .env: {e}")
        print("       Make sure .env exists and contains FEISHU_APP_ID, FEISHU_APP_SECRET, etc.")
        return

    async with httpx.AsyncClient(timeout=15.0) as http:
        # ------------------------------------------------------------------
        # Test 1: TokenManager
        # ------------------------------------------------------------------
        print("\n=== Test 1: TokenManager ===")
        manager = TokenManager(http, config.feishu_app_id, config.feishu_app_secret)
        try:
            await manager.start()
            token = await manager.get_token()
            print(f"[PASS] token obtained: {token[:20]}...")
        except Exception as e:
            print(f"[FAIL] TokenManager.start(): {e}")
            return
        finally:
            await manager.stop()

        # ------------------------------------------------------------------
        # Test 2: FeishuClient (authenticated GET — list bot info)
        # ------------------------------------------------------------------
        print("\n=== Test 2: FeishuClient (GET /bot/v3/info) ===")
        await manager.start()  # restart for subsequent tests
        client = FeishuClient(manager, http)
        try:
            data = await client.get("/bot/v3/info")
            bot_name = data.get("bot", {}).get("app_name", "unknown")
            print(f"[PASS] Bot info received: app_name={bot_name}")
        except Exception as e:
            print(f"[FAIL] FeishuClient.get(): {e}")

        # ------------------------------------------------------------------
        # Test 3: Messaging.send_text
        # ------------------------------------------------------------------
        print("\n=== Test 3: Messaging.send_text ===")
        if not TARGET_RECEIVE_ID:
            print("[SKIP] Set TEST_RECEIVE_ID env var to a chat_id or open_id to test sending.")
        else:
            messaging = Messaging(client)
            try:
                msg_id = await messaging.send_text(
                    TARGET_RECEIVE_ID,
                    "[AutoMyFeishu] Step 2/3/4 integration test — Messaging works!",
                    receive_id_type=TARGET_RECEIVE_ID_TYPE,
                )
                print(f"[PASS] Message sent; message_id={msg_id}")
            except Exception as e:
                print(f"[FAIL] Messaging.send_text(): {e}")

        await manager.stop()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
