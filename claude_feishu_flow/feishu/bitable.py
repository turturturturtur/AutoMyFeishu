"""Feishu Bitable (multi-dimensional table) read/write operations.

Each experiment gets its own dedicated table created dynamically at launch time.
Tables are created inside the user's personal Bitable app (bound via /bind command).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from claude_feishu_flow.feishu.client import FeishuClient

logger = logging.getLogger(__name__)

# Schema for per-experiment metric/log tables.
# type 1 = 多行文本 (multi-line text), type 2 = 数字 (number)
_EXPERIMENT_FIELDS: list[dict[str, Any]] = [
    {"field_name": "Epoch_Step",  "type": 2},  # Number — training step or epoch index
    {"field_name": "Metric_Name", "type": 1},  # Text  — e.g. "loss", "accuracy", "run_summary"
    {"field_name": "Value",       "type": 2},  # Number — metric value
    {"field_name": "Log_Message", "type": 1},  # Text  — free-form log / summary text
    {"field_name": "Timestamp",   "type": 1},  # Text  — ISO-8601 timestamp
]


class BitableClient:
    """Read and write records in a Feishu Bitable table.

    Tokens (app_token, table_id) are passed per-call rather than stored at
    construction time, enabling multi-user / multi-table operation without
    creating a new client instance per user.

    Typical usage:
        bitable = BitableClient(feishu_client)
        table_id = await bitable.create_experiment_table(app_token, "ViT_CIFAR10")
        await bitable.append_record(app_token, table_id, {...})
    """

    def __init__(self, client: FeishuClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Table lifecycle
    # ------------------------------------------------------------------

    async def create_experiment_table(self, app_token: str, table_name: str) -> str:
        """Create a new table inside the user's Bitable and initialise its schema.

        Args:
            app_token:  The user's Bitable app token (e.g. "bascXXXXX").
            table_name: Name for the new table — typically the experiment alias or task_id.

        Returns:
            The new table_id (e.g. "tblXXXXX").
        """
        path = f"/bitable/v1/apps/{app_token}/tables"
        data = await self._client.post(path, {"table": {"name": table_name}})
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable create_table failed: code={data.get('code')} msg={data.get('msg')}"
            )
        table_id: str = data["data"]["table_id"]
        logger.info("Created Bitable table '%s' table_id=%s", table_name, table_id)

        await self._init_fields(app_token, table_id)
        return table_id

    async def _init_fields(self, app_token: str, table_id: str) -> None:
        """Create the standard metric/log columns on a freshly created table."""
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        for field in _EXPERIMENT_FIELDS:
            data = await self._client.post(path, field)
            if data.get("code") != 0:
                raise RuntimeError(
                    f"Bitable create_field '{field['field_name']}' failed: "
                    f"code={data.get('code')} msg={data.get('msg')}"
                )
            logger.debug("Created field '%s' (type %d)", field["field_name"], field["type"])

    # ------------------------------------------------------------------
    # Record operations
    # ------------------------------------------------------------------

    async def append_record(
        self, app_token: str, table_id: str, fields: dict[str, Any]
    ) -> str:
        """Create a single record in the given table.

        Args:
            app_token: The Bitable app token.
            table_id:  The target table_id.
            fields:    Mapping of column name → value.

        Returns:
            The new record_id (e.g. "recABCD1234").
        """
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        data = await self._client.post(path, {"fields": fields})
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable append_record failed: code={data.get('code')} msg={data.get('msg')}"
            )
        record_id: str = data["data"]["record"]["record_id"]
        logger.info(
            "Appended Bitable record record_id=%s table=%s", record_id, table_id
        )
        return record_id

    async def list_records(
        self,
        app_token: str,
        table_id: str,
        filter_expr: Optional[str] = None,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch a page of records from the given table.

        Args:
            app_token:   The Bitable app token.
            table_id:    The target table_id.
            filter_expr: Optional Bitable filter formula.
            page_size:   Records per page (max 100).
            page_token:  Pagination cursor from a previous response.

        Returns:
            List of record dicts with keys "record_id" and "fields".
        """
        params: dict[str, Any] = {"page_size": page_size}
        if filter_expr:
            params["filter"] = filter_expr
        if page_token:
            params["page_token"] = page_token

        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        data = await self._client.get(path, params=params)
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable list_records failed: code={data.get('code')} msg={data.get('msg')}"
            )
        items: list[dict[str, Any]] = data.get("data", {}).get("items", [])
        logger.info("Fetched %d Bitable records from table=%s", len(items), table_id)
        return items
