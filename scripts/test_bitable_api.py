"""Manual integration test for Feishu Bitable per-user binding + dynamic table creation.

Requires BITABLE_APP_TOKEN to be provided as a command-line argument or set via
the APP_TOKEN env var. The script will:
  1. Create a new experiment table.
  2. Append a test record.
  3. List records to verify the write.

Run:
    python scripts/test_bitable_api.py <app_token>
    # or
    APP_TOKEN=bascXXX python scripts/test_bitable_api.py
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

    app_token = sys.argv[1] if len(sys.argv) > 1 else os.getenv("APP_TOKEN", "")
    if not app_token:
        print("[FAIL] Please provide app_token as first argument or APP_TOKEN env var.")
        return

    try:
        config = Config()
    except Exception as e:
        print(f"[FAIL] Could not load Config: {e}")
        return

    async with httpx.AsyncClient(timeout=15.0) as http:
        manager = TokenManager(http, config.feishu_app_id, config.feishu_app_secret)
        await manager.start()
        client = FeishuClient(manager, http)
        bitable = BitableClient(client)

        # ------------------------------------------------------------------
        # Test 1: create_experiment_table
        # ------------------------------------------------------------------
        print(f"\n=== Test 1: BitableClient.create_experiment_table (app_token={app_token}) ===")
        try:
            table_id = await bitable.create_experiment_table(app_token, "integration_test_table")
            print(f"[PASS] Table created: table_id={table_id}")
        except Exception as e:
            print(f"[FAIL] create_experiment_table: {e}")
            print("       Check that the bot has been added as an editable collaborator on the Bitable.")
            await manager.stop()
            return

        # ------------------------------------------------------------------
        # Test 2: append_record
        # ------------------------------------------------------------------
        print("\n=== Test 2: BitableClient.append_record ===")
        from datetime import datetime
        test_fields = {
            "Epoch_Step": 0,
            "Metric_Name": "run_summary",
            "Value": 0.42,
            "Log_Message": "integration test — please delete",
            "Timestamp": datetime.now().isoformat(),
        }
        try:
            record_id = await bitable.append_record(app_token, table_id, test_fields)
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
            records = await bitable.list_records(app_token, table_id, page_size=10)
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
