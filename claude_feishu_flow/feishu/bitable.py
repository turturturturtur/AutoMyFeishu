"""Feishu Bitable (multi-dimensional table) read/write and auto-schema operations."""

from __future__ import annotations

import logging
from typing import Any, Optional

from claude_feishu_flow.feishu.client import FeishuClient

logger = logging.getLogger(__name__)

TABLE_NAME = "Experiment_Results"

# Schema for the experiment log table.
# type 1 = 多行文本 (multi-line text), type 2 = 数字 (number)
_EXPERIMENT_FIELDS: list[dict[str, Any]] = [
    {"field_name": "Command",    "type": 1},
    {"field_name": "TaskID",     "type": 1},
    {"field_name": "ScriptPath", "type": 1},
    {"field_name": "Status",     "type": 1},
    {"field_name": "Duration_s", "type": 2},
    {"field_name": "Stdout",     "type": 1},
    {"field_name": "Stderr",     "type": 1},
    {"field_name": "PlanPath",   "type": 1},
    {"field_name": "LogPath",    "type": 1},
    {"field_name": "ResultSummary", "type": 1},  # Claude-generated markdown analysis
]


class BitableClient:
    """Read and write records in a Feishu Bitable table, with auto-schema init.

    Typical usage at app startup:
        bitable = BitableClient(feishu_client, app_token)
        await bitable.ensure_experiment_table()   # idempotent; finds or creates table+schema
        # bitable is now ready to use

    Or, if you already know the table_id:
        bitable = BitableClient(feishu_client, app_token, table_id="tblXXX")
    """

    def __init__(
        self,
        client: FeishuClient,
        app_token: str,
        table_id: str = "",
    ) -> None:
        self._client = client
        self._app_token = app_token
        self._table_id = table_id

    @property
    def table_id(self) -> str:
        return self._table_id

    @property
    def _base_path(self) -> str:
        if not self._table_id:
            raise RuntimeError(
                "BitableClient.table_id is not set. "
                "Call await bitable.ensure_experiment_table() first."
            )
        return f"/bitable/v1/apps/{self._app_token}/tables/{self._table_id}"

    # ------------------------------------------------------------------
    # Auto-schema initialisation
    # ------------------------------------------------------------------

    async def ensure_experiment_table(self) -> str:
        """Find or create the 'Experiment_Results' table and its 7 columns.

        Idempotent: safe to call on every startup. If the table already exists
        its table_id is reused and no schema changes are made.

        Returns:
            The table_id (also stored in self._table_id for subsequent calls).
        """
        table_id = await self._find_table(TABLE_NAME)

        if table_id:
            logger.info("Found existing Bitable table '%s' id=%s", TABLE_NAME, table_id)
        else:
            logger.info("Table '%s' not found; creating it...", TABLE_NAME)
            table_id = await self._create_table(TABLE_NAME)
            await self._create_fields(table_id)
            logger.info("Created table '%s' id=%s with %d fields", TABLE_NAME, table_id, len(_EXPERIMENT_FIELDS))

        self._table_id = table_id
        return table_id

    async def _find_table(self, name: str) -> Optional[str]:
        """Return table_id of a table with the given name, or None."""
        data = await self._client.get(
            f"/bitable/v1/apps/{self._app_token}/tables",
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable list_tables failed: code={data.get('code')} msg={data.get('msg')}"
            )
        items: list[dict] = data.get("data", {}).get("items", [])
        for item in items:
            if item.get("name") == name:
                return item["table_id"]
        return None

    async def _create_table(self, name: str) -> str:
        """Create a new table and return its table_id."""
        data = await self._client.post(
            f"/bitable/v1/apps/{self._app_token}/tables",
            {"table": {"name": name}},
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable create_table failed: code={data.get('code')} msg={data.get('msg')}"
            )
        return data["data"]["table_id"]

    async def _create_fields(self, table_id: str) -> None:
        """Create all experiment schema fields on a freshly created table."""
        path = f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/fields"
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

    async def append_record(self, fields: dict[str, Any]) -> str:
        """Create a single record in the table.

        Args:
            fields: Mapping of column name → value.

        Returns:
            The new record_id (e.g. "recABCD1234").
        """
        data = await self._client.post(
            f"{self._base_path}/records",
            {"fields": fields},
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable append_record failed: code={data.get('code')} msg={data.get('msg')}"
            )
        record_id: str = data["data"]["record"]["record_id"]
        logger.info("Appended Bitable record record_id=%s table=%s", record_id, self._table_id)
        return record_id

    async def list_records(
        self,
        filter_expr: Optional[str] = None,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch a page of records from the table.

        Args:
            filter_expr: Optional Bitable filter formula, e.g.
                         'CurrentValue.[Status] = "success"'.
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

        data = await self._client.get(f"{self._base_path}/records", params=params)
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bitable list_records failed: code={data.get('code')} msg={data.get('msg')}"
            )
        items: list[dict[str, Any]] = data.get("data", {}).get("items", [])
        logger.info("Fetched %d Bitable records from table=%s", len(items), self._table_id)
        return items
