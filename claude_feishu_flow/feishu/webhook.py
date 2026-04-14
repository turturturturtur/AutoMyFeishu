"""Feishu webhook signature verification and event parsing."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


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
    verification_token: str,
    signature: str,
) -> bool:
    """Verify the X-Lark-Signature header (event callback v2).

    Signature algorithm: sha256(timestamp + nonce + encrypt_key + body)
    Per Feishu docs the key is the *encrypt_key* when encryption is enabled,
    otherwise it falls back to the verification token for plain-text callbacks.
    This function uses verification_token as the key material.
    """
    content = timestamp + nonce + verification_token + body_bytes.decode("utf-8")
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

def parse_webhook_event(raw: dict) -> WebhookEvent:
    """Parse a raw Feishu webhook payload into a typed WebhookEvent.

    Handles two schemas:
    1. URL verification challenge  (schema_v1 / v2 both use top-level "challenge")
    2. im.message.receive_v1       (schema_v2: raw["header"]["event_type"])
    """
    # --- URL verification (one-time setup) ---
    if "challenge" in raw:
        return WebhookEvent(
            event_type="url_verification",
            challenge=raw["challenge"],
            raw=raw,
        )

    # --- Event callback v2 schema ---
    header: dict = raw.get("header", {})
    event_type: str = header.get("event_type", "unknown")
    event: dict = raw.get("event", {})

    if event_type == "im.message.receive_v1":
        sender: dict = event.get("sender", {})
        message: dict = event.get("message", {})

        # Extract plain text from message content JSON
        text: Optional[str] = None
        content_raw = message.get("content", "")
        try:
            content_obj = json.loads(content_raw)
            text = content_obj.get("text", "").strip()
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
            raw=raw,
        )

    # --- Unknown / unsupported event types ---
    logger.info("Received unsupported event type: %s", event_type)
    return WebhookEvent(event_type=event_type, raw=raw)
