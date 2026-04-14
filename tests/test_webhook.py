"""Unit tests for feishu.webhook (signature verification and event parsing)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from claude_feishu_flow.feishu.webhook import (
    WebhookEvent,
    decrypt_feishu_message,
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


# ---------------------------------------------------------------------------
# AES-256-CBC encryption / decryption round-trip
# ---------------------------------------------------------------------------

def _encrypt_feishu_message(payload: dict, encrypt_key: str) -> str:
    """Encrypt a dict the same way Feishu does, for testing purposes."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    import os

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = os.urandom(16)
    plaintext = json.dumps(payload).encode("utf-8")

    # PKCS7 padding
    pad_len = 16 - (len(plaintext) % 16)
    plaintext += bytes([pad_len] * pad_len)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(plaintext) + encryptor.finalize()

    return base64.b64encode(iv + encrypted).decode("utf-8")


def test_decrypt_url_verification_challenge():
    """Decrypting an encrypted url_verification payload returns the challenge."""
    key = "test_encrypt_key_abc"
    payload = {"challenge": "ch_encrypted_xyz", "type": "url_verification"}
    encrypted = _encrypt_feishu_message(payload, key)

    result = decrypt_feishu_message(encrypted, key)
    assert result["challenge"] == "ch_encrypted_xyz"


def test_decrypt_message_event():
    """Decrypting an encrypted message event returns full structure."""
    key = "myEncryptKey123"
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_u1"}},
            "message": {
                "message_id": "om_enc001",
                "chat_id": "oc_enc001",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "hello encrypted"}),
            },
        },
    }
    encrypted = _encrypt_feishu_message(payload, key)
    result = decrypt_feishu_message(encrypted, key)
    assert result["header"]["event_type"] == "im.message.receive_v1"
    assert result["event"]["message"]["message_id"] == "om_enc001"


def test_decrypt_then_parse_url_verification():
    """Full pipeline: decrypt encrypted body → parse_webhook_event → url_verification."""
    key = "pipelineKey"
    payload = {"challenge": "pipeline_challenge", "type": "url_verification"}
    encrypted = _encrypt_feishu_message(payload, key)

    decrypted = decrypt_feishu_message(encrypted, key)
    event = parse_webhook_event(decrypted)
    assert event.event_type == "url_verification"
    assert event.challenge == "pipeline_challenge"


def test_decrypt_wrong_key_raises():
    """Using the wrong key should raise an exception (bad padding or JSON)."""
    payload = {"challenge": "xyz"}
    encrypted = _encrypt_feishu_message(payload, "correct_key")
    with pytest.raises(Exception):
        decrypt_feishu_message(encrypted, "wrong_key")

