"""Unit tests for feishu.webhook (signature verification and event parsing)."""

from __future__ import annotations

import hashlib
import json

import pytest

from claude_feishu_flow.feishu.webhook import (
    WebhookEvent,
    parse_webhook_event,
    verify_feishu_signature_v2,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signature(timestamp: str, nonce: str, token: str, body: bytes) -> str:
    content = timestamp + nonce + token + body.decode("utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Signature verification tests
# ---------------------------------------------------------------------------

def test_valid_signature():
    token = "my_verification_token"
    timestamp = "1700000000"
    nonce = "abc123"
    body = b'{"type":"event_callback"}'
    sig = _make_signature(timestamp, nonce, token, body)
    assert verify_feishu_signature_v2(timestamp, nonce, body, token, sig) is True


def test_invalid_signature():
    token = "my_verification_token"
    timestamp = "1700000000"
    nonce = "abc123"
    body = b'{"type":"event_callback"}'
    assert verify_feishu_signature_v2(timestamp, nonce, body, token, "badhash") is False


def test_tampered_body_fails_verification():
    token = "my_verification_token"
    timestamp = "1700000000"
    nonce = "abc123"
    original_body = b'{"type":"event_callback"}'
    sig = _make_signature(timestamp, nonce, token, original_body)
    tampered_body = b'{"type":"evil_payload"}'
    assert verify_feishu_signature_v2(timestamp, nonce, tampered_body, token, sig) is False


# ---------------------------------------------------------------------------
# URL verification challenge
# ---------------------------------------------------------------------------

def test_parse_url_verification():
    raw = {"challenge": "challenge_string_xyz", "token": "verify_token", "type": "url_verification"}
    event = parse_webhook_event(raw)
    assert event.event_type == "url_verification"
    assert event.challenge == "challenge_string_xyz"


# ---------------------------------------------------------------------------
# im.message.receive_v1 parsing
# ---------------------------------------------------------------------------

def _make_message_event(text: str = "hello world", chat_type: str = "group") -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_001",
            "event_type": "im.message.receive_v1",
            "create_time": "1700000000000",
            "token": "verify_token",
            "app_id": "cli_abc",
            "tenant_key": "tenant123",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user001", "user_id": "user001"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_msg001",
                "root_id": "",
                "parent_id": "",
                "create_time": "1700000000000",
                "chat_id": "oc_chat001",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_parse_message_event_text():
    raw = _make_message_event("run experiment 1")
    event = parse_webhook_event(raw)
    assert event.event_type == "im.message.receive_v1"
    assert event.text == "run experiment 1"
    assert event.message_id == "om_msg001"
    assert event.chat_id == "oc_chat001"
    assert event.open_id == "ou_user001"
    assert event.chat_type == "group"


def test_parse_message_event_strips_whitespace():
    raw = _make_message_event("  hello  ")
    event = parse_webhook_event(raw)
    assert event.text == "hello"


def test_parse_message_event_p2p():
    raw = _make_message_event("ping", chat_type="p2p")
    event = parse_webhook_event(raw)
    assert event.chat_type == "p2p"


def test_parse_unknown_event_type():
    raw = {
        "schema": "2.0",
        "header": {"event_type": "contact.user.created_v3"},
        "event": {},
    }
    event = parse_webhook_event(raw)
    assert event.event_type == "contact.user.created_v3"
    assert event.text is None
    assert event.challenge is None


def test_parse_message_bad_content_json():
    """Non-JSON content should fall back to raw string without crashing."""
    raw = _make_message_event()
    # Replace with invalid JSON
    raw["event"]["message"]["content"] = "not-json"
    event = parse_webhook_event(raw)
    assert event.text == "not-json"
