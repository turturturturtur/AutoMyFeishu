"""Unit tests for feishu.bitable (BitableClient)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from claude_feishu_flow.feishu.bitable import BitableClient, _EXPERIMENT_FIELDS
from claude_feishu_flow.feishu.client import FeishuClient

APP_TOKEN = "bascABCDEFGH"
TABLE_ID = "tbl12345678"
BASE_PATH = f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}"


def _mock_client() -> FeishuClient:
    client = AsyncMock(spec=FeishuClient)
    return client


# ---------------------------------------------------------------------------
# create_experiment_table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_experiment_table_returns_table_id():
    """create_experiment_table() creates table, inits fields, returns table_id."""
    client = _mock_client()
    create_table_resp = {"code": 0, "data": {"table_id": TABLE_ID}}
    field_resp = {"code": 0, "data": {"field": {"field_id": "fldXXX"}}}
    client.post = AsyncMock(side_effect=[create_table_resp] + [field_resp] * len(_EXPERIMENT_FIELDS))

    bitable = BitableClient(client)
    table_id = await bitable.create_experiment_table(APP_TOKEN, "ViT_CIFAR10")

    assert table_id == TABLE_ID
    # 1 table create + N field creates
    assert client.post.call_count == 1 + len(_EXPERIMENT_FIELDS)


@pytest.mark.asyncio
async def test_create_experiment_table_posts_to_correct_path():
    client = _mock_client()
    create_table_resp = {"code": 0, "data": {"table_id": TABLE_ID}}
    field_resp = {"code": 0, "data": {"field": {}}}
    client.post = AsyncMock(side_effect=[create_table_resp] + [field_resp] * len(_EXPERIMENT_FIELDS))

    bitable = BitableClient(client)
    await bitable.create_experiment_table(APP_TOKEN, "MyExp")

    first_call_path = client.post.call_args_list[0].args[0]
    assert first_call_path == f"/bitable/v1/apps/{APP_TOKEN}/tables"


@pytest.mark.asyncio
async def test_create_experiment_table_inits_correct_fields():
    """Fields are posted in the right order with correct types."""
    client = _mock_client()
    create_table_resp = {"code": 0, "data": {"table_id": TABLE_ID}}
    field_resp = {"code": 0, "data": {"field": {}}}
    client.post = AsyncMock(side_effect=[create_table_resp] + [field_resp] * len(_EXPERIMENT_FIELDS))

    bitable = BitableClient(client)
    await bitable.create_experiment_table(APP_TOKEN, "MyExp")

    field_calls = client.post.call_args_list[1:]
    posted_fields = [c.args[1] for c in field_calls]
    assert posted_fields == _EXPERIMENT_FIELDS


@pytest.mark.asyncio
async def test_create_experiment_table_raises_on_api_error():
    client = _mock_client()
    client.post = AsyncMock(return_value={"code": 99, "msg": "quota exceeded"})

    bitable = BitableClient(client)
    with pytest.raises(RuntimeError, match="Bitable create_table failed"):
        await bitable.create_experiment_table(APP_TOKEN, "FailExp")


# ---------------------------------------------------------------------------
# append_record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_record_returns_record_id():
    client = _mock_client()
    client.post = AsyncMock(return_value={
        "code": 0,
        "data": {"record": {"record_id": "recABC001", "fields": {}}},
    })
    bitable = BitableClient(client)

    record_id = await bitable.append_record(APP_TOKEN, TABLE_ID, {"Metric_Name": "loss", "Value": 0.5})
    assert record_id == "recABC001"


@pytest.mark.asyncio
async def test_append_record_posts_to_correct_path():
    client = _mock_client()
    client.post = AsyncMock(return_value={
        "code": 0,
        "data": {"record": {"record_id": "recXYZ", "fields": {}}},
    })
    bitable = BitableClient(client)

    fields = {"Metric_Name": "accuracy", "Value": 0.95}
    await bitable.append_record(APP_TOKEN, TABLE_ID, fields)

    path = client.post.call_args.args[0]
    payload = client.post.call_args.args[1]
    assert path == f"{BASE_PATH}/records"
    assert payload["fields"] == fields


@pytest.mark.asyncio
async def test_append_record_raises_on_api_error():
    client = _mock_client()
    client.post = AsyncMock(return_value={"code": 1254016, "msg": "FieldName not found"})
    bitable = BitableClient(client)

    with pytest.raises(RuntimeError, match="Bitable append_record failed"):
        await bitable.append_record(APP_TOKEN, TABLE_ID, {"BadField": "x"})


# ---------------------------------------------------------------------------
# list_records
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_records_returns_items():
    client = _mock_client()
    client.get = AsyncMock(return_value={
        "code": 0,
        "data": {
            "items": [
                {"record_id": "rec001", "fields": {"Metric_Name": "loss"}},
                {"record_id": "rec002", "fields": {"Metric_Name": "acc"}},
            ],
        },
    })
    bitable = BitableClient(client)

    records = await bitable.list_records(APP_TOKEN, TABLE_ID)
    assert len(records) == 2
    assert records[0]["record_id"] == "rec001"


@pytest.mark.asyncio
async def test_list_records_passes_filter_and_page_size():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {"items": []}})
    bitable = BitableClient(client)

    await bitable.list_records(APP_TOKEN, TABLE_ID, filter_expr='CurrentValue.[Metric_Name] = "loss"', page_size=50)

    params = client.get.call_args.kwargs.get("params", {})
    assert params["filter"] == 'CurrentValue.[Metric_Name] = "loss"'
    assert params["page_size"] == 50


@pytest.mark.asyncio
async def test_list_records_passes_page_token():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {"items": []}})
    bitable = BitableClient(client)

    await bitable.list_records(APP_TOKEN, TABLE_ID, page_token="token_xyz")

    params = client.get.call_args.kwargs.get("params", {})
    assert params["page_token"] == "token_xyz"


@pytest.mark.asyncio
async def test_list_records_returns_empty_on_no_items():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {}})
    bitable = BitableClient(client)

    assert await bitable.list_records(APP_TOKEN, TABLE_ID) == []


@pytest.mark.asyncio
async def test_list_records_raises_on_api_error():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 1254001, "msg": "table not found"})
    bitable = BitableClient(client)

    with pytest.raises(RuntimeError, match="Bitable list_records failed"):
        await bitable.list_records(APP_TOKEN, TABLE_ID)
