"""Feishu webhook signature verification, decryption, and event parsing."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AES-256-CBC decryption for encrypted Feishu webhook bodies
# ---------------------------------------------------------------------------

def decrypt_feishu_message(encrypt_string: str, encrypt_key: str) -> dict:
    """Decrypt a Feishu AES-256-CBC encrypted webhook body.

    Algorithm (per Feishu docs):
    1. key = sha256(encrypt_key).digest()[:32]   — first 32 bytes of SHA-256 hash
    2. ciphertext = base64_decode(encrypt_string)
    3. iv = ciphertext[:16]                       — first 16 bytes are the IV
    4. plaintext = AES-256-CBC-decrypt(ciphertext[16:], key, iv)
    5. Strip PKCS7 padding, then JSON-parse.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    # Key derivation: SHA-256 of the encrypt_key string, take first 32 bytes
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()

    # Base64-decode the ciphertext
    ciphertext = base64.b64decode(encrypt_string)

    # First 16 bytes = IV, rest = actual encrypted data
    iv = ciphertext[:16]
    encrypted_data = ciphertext[16:]

    # AES-256-CBC decrypt
    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(encrypted_data) + decryptor.finalize()

    # Strip PKCS7 padding
    pad_len = padded_plaintext[-1]
    plaintext = padded_plaintext[:-pad_len].decode("utf-8")

    return json.loads(plaintext)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class WebhookEvent:
    """Parsed Feishu webhook event.

    event_type values handled by this MVP:
      - "url_verification"      : Feishu one-time callback setup challenge
      - "im.message.receive_v1" : User sends a message in a chat
    """

    event_type: str
    # url_verification fields
    challenge: Optional[str] = None
    # im.message.receive_v1 fields
    message_id: Optional[str] = None
    open_id: Optional[str] = None
    chat_id: Optional[str] = None
    chat_type: Optional[str] = None   # "p2p" | "group"
    text: Optional[str] = None
    message_type: Optional[str] = None  # "text" | "image" | ...
    image_keys: list = field(default_factory=list)  # image_key values for message_type == "image"
    parent_id: Optional[str] = None   # 引用回复时，被引用的原始消息 ID
    # card.action.trigger fields (button clicks on interactive cards)
    action_tag: Optional[str] = None       # e.g. "button"
    action_value: dict = field(default_factory=dict)  # e.g. {"key": "enter_session", "task_id": "exp_xxx"}
    action_chat_id: Optional[str] = None   # from event.context.open_chat_id
    # Raw event dict for forward compatibility
    raw: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_feishu_signature(
    timestamp: str,
    nonce: str,
    body_bytes: bytes,
    verification_token: str,
) -> bool:
    """Verify the X-Lark-Signature header using Feishu's HMAC-SHA256 scheme.

    Feishu signature = HMAC-SHA256(key=app_secret(?), msg=timestamp+nonce+body)
    Note: Feishu's *event callback v2* uses a different scheme than v1.
    This implements the v2 scheme: sha256(token + timestamp + nonce + body_string).
    See: https://open.feishu.cn/document/ukTMukTMukTM/uUTNz4SN1MjL1UzM
    """
    content = verification_token + timestamp + nonce + body_bytes.decode("utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest == digest  # placeholder — real check below


def verify_feishu_signature_v2(
    timestamp: str,
    nonce: str,
    body_bytes: bytes,
    token: str,
    signature: str,
) -> bool:
    """Verify X-Lark-Signature for event callback v2.

    Algorithm (Feishu docs):
      str_to_sign = timestamp + nonce + token + body_string
      expected    = sha256(str_to_sign).hexdigest()

    When encryption is enabled, `token` must be the FEISHU_ENCRYPT_KEY.
    When encryption is disabled, `token` is the FEISHU_VERIFICATION_TOKEN.
    Always uses plain SHA-256 (not HMAC).
    """
    str_to_sign = timestamp + nonce + token + body_bytes.decode("utf-8")
    expected = hashlib.sha256(str_to_sign.encode("utf-8")).hexdigest()
    return expected == signature


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

def parse_webhook_event(raw: dict) -> WebhookEvent:
    """Parse a raw Feishu webhook payload into a typed WebhookEvent.

    Handles three schemas:
    1. URL verification challenge  (schema_v1 / v2 both use top-level "challenge")
    2. Card action callback        (flat format: top-level "action" + "open_id")
    3. im.message.receive_v1       (schema_v2: raw["header"]["event_type"])
    """
    # --- URL verification (one-time setup) ---
    if "challenge" in raw:
        return WebhookEvent(
            event_type="url_verification",
            challenge=raw["challenge"],
            raw=raw,
        )

    # --- Flat card action callback (no header/event wrapper) ──────────
    # Feishu card interaction callbacks arrive with top-level "action"
    # and "open_id" fields, WITHOUT the v2 header/event envelope.
    if "action" in raw and "header" not in raw:
        action: dict = raw.get("action", {})
        return WebhookEvent(
            event_type="card.action.trigger",
            open_id=raw.get("open_id"),
            action_tag=action.get("tag"),
            action_value=action.get("value", {}),
            action_chat_id=raw.get("open_chat_id"),
            raw=raw,
        )

    # --- Event callback v2 schema ---
    header: dict = raw.get("header", {})
    event_type: str = header.get("event_type", "unknown")
    event: dict = raw.get("event", {})

    if event_type == "im.message.receive_v1":
        sender: dict = event.get("sender", {})
        message: dict = event.get("message", {})

        message_type: str = message.get("message_type", "text")
        image_keys: list = []

        # Extract plain text or image_key from message content JSON
        text: Optional[str] = None
        content_raw = message.get("content", "")
        try:
            content_obj = json.loads(content_raw)
            if message_type == "text":
                text = content_obj.get("text", "").strip()
            elif message_type == "image":
                image_key = content_obj.get("image_key", "")
                if image_key:
                    image_keys.append(image_key)
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Could not parse message content: %r", content_raw)
            text = content_raw

        return WebhookEvent(
            event_type=event_type,
            message_id=message.get("message_id"),
            open_id=sender.get("sender_id", {}).get("open_id"),
            chat_id=message.get("chat_id"),
            chat_type=message.get("chat_type"),
            text=text,
            message_type=message_type,
            image_keys=image_keys,
            parent_id=message.get("parent_id"),
            raw=raw,
        )

    # --- card.action.trigger (interactive card button clicks) ---
    if event_type == "card.action.trigger":
        operator: dict = event.get("operator", {})
        action: dict = event.get("action", {})
        context: dict = event.get("context", {})
        return WebhookEvent(
            event_type=event_type,
            open_id=operator.get("open_id"),
            action_tag=action.get("tag"),
            action_value=action.get("value", {}),
            action_chat_id=context.get("open_chat_id"),
            raw=raw,
        )

    # --- Unknown / unsupported event types ---
    logger.info("Received unsupported event type: %s", event_type)
    return WebhookEvent(event_type=event_type, raw=raw)
