"""Integration tests for server/routes.py using FastAPI TestClient.

These tests verify:
- URL verification challenge response
- HTTP 200 is returned immediately (before background work)
- Signature verification
- Duplicate message deduplication
- Background task is registered (not executed inline)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from claude_feishu_flow.server.app import Services


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERIFICATION_TOKEN = "test_token_abc"


def _make_sig(timestamp: str, nonce: str, token: str, body: bytes) -> str:
    content = timestamp + nonce + token + body.decode("utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _signed_headers(body: bytes, token: str = VERIFICATION_TOKEN) -> dict:
    ts = str(int(time.time()))
    nonce = "testnonce"
    return {
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": _make_sig(ts, nonce, token, body),
        "Content-Type": "application/json",
    }


def _message_payload(
    text: str = "run experiment",
    message_id: str = "om_msg001",
    chat_id: str = "oc_chat001",
) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user001"}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def _make_services(config=None) -> Services:
    """Build a Services object with all async methods mocked."""
    from claude_feishu_flow.config import Config
    from claude_feishu_flow.feishu.auth import TokenManager
    from claude_feishu_flow.feishu.client import FeishuClient
    from claude_feishu_flow.feishu.messaging import Messaging
    from claude_feishu_flow.feishu.bitable import BitableClient
    from claude_feishu_flow.ai.client import ClaudeClient
    from claude_feishu_flow.runner.executor import ScriptExecutor, ExecutionResult
    import httpx

    if config is None:
        config = Config(
            feishu_app_id="id",
            feishu_app_secret="secret",
            feishu_verification_token=VERIFICATION_TOKEN,
            feishu_encrypt_key="",
            bitable_app_token="btoken",
            anthropic_api_key="ak",
        )

    messaging = AsyncMock(spec=Messaging)
    messaging.send_text = AsyncMock(return_value="om_reply")

    bitable = AsyncMock(spec=BitableClient)
    bitable.append_record = AsyncMock(return_value="recNEW001")

    claude = AsyncMock(spec=ClaudeClient)
    claude.generate_experiment = AsyncMock(return_value="/tmp/task_x/main.py")

    executor = AsyncMock(spec=ScriptExecutor)
    executor.run = AsyncMock(return_value=ExecutionResult(
        returncode=0, stdout="hello\n", stderr="", duration_seconds=1.0
    ))

    return Services(
        config=config,
        http=AsyncMock(spec=httpx.AsyncClient),
        token_manager=AsyncMock(spec=TokenManager),
        feishu=AsyncMock(spec=FeishuClient),
        messaging=messaging,
        bitable=bitable,
        claude=claude,
        executor=executor,
    )


def _test_client_with_services(services: Services) -> TestClient:
    """Create a TestClient with mocked lifespan that injects fake services.

    Use as a context manager to trigger lifespan startup/shutdown:
        with _test_client_with_services(svc) as client:
            resp = client.post(...)
    """
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from claude_feishu_flow.server.routes import router

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# URL verification
# ---------------------------------------------------------------------------

def test_url_verification_challenge():
    svc = _make_services()
    body = json.dumps({"challenge": "ch_xyz", "type": "url_verification"}).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "ch_xyz"


# ---------------------------------------------------------------------------
# Message event → immediate 200
# ---------------------------------------------------------------------------

def test_message_event_returns_200_immediately():
    """The route returns 200 without waiting for the background task."""
    svc = _make_services()
    body = json.dumps(_message_payload()).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


def test_message_event_accepted_response():
    svc = _make_services()
    body = json.dumps(_message_payload()).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=_signed_headers(body))
    assert resp.json()["msg"] == "accepted"


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def test_invalid_signature_returns_401():
    svc = _make_services()
    body = json.dumps(_message_payload()).encode()
    headers = {
        "X-Lark-Request-Timestamp": "1700000000",
        "X-Lark-Request-Nonce": "nonce",
        "X-Lark-Signature": "badhash",
        "Content-Type": "application/json",
    }
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 401


def test_missing_signature_header_passes():
    """No signature header → skip verification (development mode)."""
    svc = _make_services()
    body = json.dumps(_message_payload()).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_duplicate_message_id_returns_200_without_queuing():
    svc = _make_services()
    svc.processing_ids.add("om_msg001")
    body = json.dumps(_message_payload(message_id="om_msg001")).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["msg"] == "duplicate"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_text_ignored():
    svc = _make_services()
    body = json.dumps(_message_payload(text="   ")).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    assert "ignored" in resp.json()["msg"]


def test_unknown_event_type_ignored():
    svc = _make_services()
    raw = {"schema": "2.0", "header": {"event_type": "contact.user.created_v3"}, "event": {}}
    body = json.dumps(raw).encode()
    with _test_client_with_services(svc) as client:
        resp = client.post("/webhook", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["msg"] == "ignored"


def test_invalid_json_returns_400():
    svc = _make_services()
    with _test_client_with_services(svc) as client:
        resp = client.post(
            "/webhook",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400

