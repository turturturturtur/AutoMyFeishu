"""Unit tests for feishu.bitable (BitableClient)."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from claude_feishu_flow.feishu.bitable import BitableClient, TABLE_NAME, _EXPERIMENT_FIELDS
from claude_feishu_flow.feishu.client import FeishuClient

APP_TOKEN = "bascABCDEFGH"
TABLE_ID = "tbl12345678"
BASE_PATH = f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}"


def _mock_client() -> FeishuClient:
    client = AsyncMock(spec=FeishuClient)
    return client


# ---------------------------------------------------------------------------
# ensure_experiment_table — table already exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_table_finds_existing():
    """ensure_experiment_table() returns existing table_id without creating anything."""
    client = _mock_client()
    client.get = AsyncMock(return_value={
        "code": 0,
        "data": {
            "items": [
                {"table_id": "tblEXIST", "name": TABLE_NAME},
                {"table_id": "tblOTHER", "name": "SomeOtherTable"},
            ]
        },
    })

    bitable = BitableClient(client, APP_TOKEN)
    table_id = await bitable.ensure_experiment_table()

    assert table_id == "tblEXIST"
    assert bitable.table_id == "tblEXIST"
    client.post.assert_not_called()   # no create calls


@pytest.mark.asyncio
async def test_ensure_table_creates_when_absent():
    """ensure_experiment_table() creates table and fields when not found."""
    client = _mock_client()

    # GET tables → empty list
    list_resp = {"code": 0, "data": {"items": []}}
    # POST new table → returns table_id
    create_table_resp = {"code": 0, "data": {"table_id": "tblNEW123"}}
    # POST each field → success
    field_resp = {"code": 0, "data": {"field": {"field_id": "fldXXX"}}}

    client.get = AsyncMock(return_value=list_resp)
    client.post = AsyncMock(side_effect=[create_table_resp] + [field_resp] * len(_EXPERIMENT_FIELDS))

    bitable = BitableClient(client, APP_TOKEN)
    table_id = await bitable.ensure_experiment_table()

    assert table_id == "tblNEW123"
    assert bitable.table_id == "tblNEW123"

    # 1 table create + 7 field creates
    assert client.post.call_count == 1 + len(_EXPERIMENT_FIELDS)


@pytest.mark.asyncio
async def test_ensure_table_creates_correct_fields():
    """ensure_experiment_table() creates fields in the right order with correct types."""
    client = _mock_client()

    client.get = AsyncMock(return_value={"code": 0, "data": {"items": []}})
    create_table_resp = {"code": 0, "data": {"table_id": "tblNEW"}}
    field_resp = {"code": 0, "data": {"field": {}}}
    client.post = AsyncMock(side_effect=[create_table_resp] + [field_resp] * len(_EXPERIMENT_FIELDS))

    bitable = BitableClient(client, APP_TOKEN)
    await bitable.ensure_experiment_table()

    # Verify each field call (calls[0] is the table create, calls[1..] are fields)
    field_calls = client.post.call_args_list[1:]
    posted_fields = [c.args[1] for c in field_calls]

    assert posted_fields == _EXPERIMENT_FIELDS  # exact order and types


@pytest.mark.asyncio
async def test_ensure_table_raises_on_list_error():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 99, "msg": "permission denied"})

    bitable = BitableClient(client, APP_TOKEN)
    with pytest.raises(RuntimeError, match="Bitable list_tables failed"):
        await bitable.ensure_experiment_table()


@pytest.mark.asyncio
async def test_ensure_table_raises_on_create_error():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {"items": []}})
    client.post = AsyncMock(return_value={"code": 99, "msg": "quota exceeded"})

    bitable = BitableClient(client, APP_TOKEN)
    with pytest.raises(RuntimeError, match="Bitable create_table failed"):
        await bitable.ensure_experiment_table()


@pytest.mark.asyncio
async def test_base_path_raises_without_table_id():
    """Accessing _base_path before ensure_experiment_table() raises clearly."""
    bitable = BitableClient(_mock_client(), APP_TOKEN)
    with pytest.raises(RuntimeError, match="ensure_experiment_table"):
        _ = bitable._base_path


# ---------------------------------------------------------------------------
# append_record tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_record_returns_record_id():
    client = _mock_client()
    client.post = AsyncMock(return_value={
        "code": 0,
        "data": {"record": {"record_id": "recABC001", "fields": {}}},
    })
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    record_id = await bitable.append_record({"Command": "run exp", "Status": "success"})
    assert record_id == "recABC001"


@pytest.mark.asyncio
async def test_append_record_posts_to_correct_path():
    client = _mock_client()
    client.post = AsyncMock(return_value={
        "code": 0,
        "data": {"record": {"record_id": "recXYZ", "fields": {}}},
    })
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    fields = {"Command": "hello", "Duration_s": 1.23}
    await bitable.append_record(fields)

    path = client.post.call_args.args[0]
    payload = client.post.call_args.args[1]
    assert path == f"{BASE_PATH}/records"
    assert payload["fields"] == fields


@pytest.mark.asyncio
async def test_append_record_raises_on_api_error():
    client = _mock_client()
    client.post = AsyncMock(return_value={"code": 1254016, "msg": "FieldName not found"})
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    with pytest.raises(RuntimeError, match="Bitable append_record failed"):
        await bitable.append_record({"BadField": "x"})


# ---------------------------------------------------------------------------
# list_records tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_records_returns_items():
    client = _mock_client()
    client.get = AsyncMock(return_value={
        "code": 0,
        "data": {
            "items": [
                {"record_id": "rec001", "fields": {"Command": "exp 1"}},
                {"record_id": "rec002", "fields": {"Command": "exp 2"}},
            ],
        },
    })
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    records = await bitable.list_records()
    assert len(records) == 2
    assert records[0]["record_id"] == "rec001"


@pytest.mark.asyncio
async def test_list_records_passes_filter_and_page_size():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {"items": []}})
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    await bitable.list_records(filter_expr='CurrentValue.[Status] = "success"', page_size=50)

    params = client.get.call_args.kwargs.get("params", {})
    assert params["filter"] == 'CurrentValue.[Status] = "success"'
    assert params["page_size"] == 50


@pytest.mark.asyncio
async def test_list_records_passes_page_token():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {"items": []}})
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    await bitable.list_records(page_token="token_xyz")

    params = client.get.call_args.kwargs.get("params", {})
    assert params["page_token"] == "token_xyz"


@pytest.mark.asyncio
async def test_list_records_returns_empty_on_no_items():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 0, "data": {}})
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    assert await bitable.list_records() == []


@pytest.mark.asyncio
async def test_list_records_raises_on_api_error():
    client = _mock_client()
    client.get = AsyncMock(return_value={"code": 1254001, "msg": "table not found"})
    bitable = BitableClient(client, APP_TOKEN, table_id=TABLE_ID)

    with pytest.raises(RuntimeError, match="Bitable list_records failed"):
        await bitable.list_records()
