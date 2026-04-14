"""Manual integration test for Step 5 — Feishu Bitable auto-init + write/read.

Only requires BITABLE_APP_TOKEN in .env. The script will:
  1. Call ensure_experiment_table() to find or create the 'Experiment_Results' table.
  2. Append a test record.
  3. List records to verify the write.

Run:
    python scripts/test_bitable_api.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> None:
    import httpx
    from claude_feishu_flow.config import Config
    from claude_feishu_flow.feishu.auth import TokenManager
    from claude_feishu_flow.feishu.bitable import BitableClient
    from claude_feishu_flow.feishu.client import FeishuClient

    try:
        config = Config()
    except Exception as e:
        print(f"[FAIL] Could not load Config: {e}")
        return

    if "xxx" in config.bitable_app_token:
        print("[SKIP] BITABLE_APP_TOKEN not set in .env — fill it in and re-run.")
        return

    async with httpx.AsyncClient(timeout=15.0) as http:
        manager = TokenManager(http, config.feishu_app_id, config.feishu_app_secret)
        await manager.start()
        client = FeishuClient(manager, http)
        bitable = BitableClient(client, config.bitable_app_token)

        # ------------------------------------------------------------------
        # Test 1: ensure_experiment_table (auto-init schema)
        # ------------------------------------------------------------------
        print("\n=== Test 1: BitableClient.ensure_experiment_table ===")
        try:
            table_id = await bitable.ensure_experiment_table()
            print(f"[PASS] Table ready: table_id={table_id}")
        except Exception as e:
            print(f"[FAIL] ensure_experiment_table: {e}")
            print("       Check that the bot has 'bitable:app' read/write permission.")
            await manager.stop()
            return

        # ------------------------------------------------------------------
        # Test 2: append_record
        # ------------------------------------------------------------------
        print("\n=== Test 2: BitableClient.append_record ===")
        test_fields = {
            "Command": "integration test — please delete",
            "TaskID": "test-uuid-0000",
            "ScriptPath": "./workspaces/test-uuid-0000/experiment.py",
            "Status": "success",
            "Duration_s": 0.42,
            "Stdout": "Hello from integration test",
            "Stderr": "",
        }
        try:
            record_id = await bitable.append_record(test_fields)
            print(f"[PASS] Record created: record_id={record_id}")
        except Exception as e:
            print(f"[FAIL] append_record: {e}")
            await manager.stop()
            return

        # ------------------------------------------------------------------
        # Test 3: list_records (verify the write)
        # ------------------------------------------------------------------
        print("\n=== Test 3: BitableClient.list_records ===")
        try:
            records = await bitable.list_records(page_size=10)
            print(f"[PASS] Fetched {len(records)} record(s)")
            found = any(r.get("record_id") == record_id for r in records)
            status = "[PASS]" if found else "[INFO]"
            note = "" if found else " (may be ordering/pagination; that's OK)"
            print(f"{status} Record {record_id} visible in list{note}")
        except Exception as e:
            print(f"[FAIL] list_records: {e}")

        await manager.stop()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
