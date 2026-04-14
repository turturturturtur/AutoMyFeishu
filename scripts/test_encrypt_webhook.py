#!/usr/bin/env python3
"""Local smoke test: simulate Feishu sending an encrypted url_verification challenge.

This script:
1. Reads FEISHU_ENCRYPT_KEY and FEISHU_VERIFICATION_TOKEN from .env
2. Builds a real AES-256-CBC encrypted challenge payload (exactly how Feishu does it)
3. POSTs it to localhost:8080/webhook
4. Asserts the server returns {"challenge": "..."} correctly

Usage:
    python scripts/test_encrypt_webhook.py

Make sure the server is running first:
    python -m claude_feishu_flow
    # or: uvicorn claude_feishu_flow.server.app:create_app --factory --port 8080
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time

# ── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; export vars manually if needed

ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080")


def encrypt_payload(payload: dict, encrypt_key: str) -> str:
    """AES-256-CBC encrypt a dict exactly as Feishu does."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

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


def make_signature(timestamp: str, nonce: str, token: str, body: bytes) -> str:
    content = timestamp + nonce + token + body.decode("utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def run_test():
    import httpx

    if not ENCRYPT_KEY:
        print("ERROR: FEISHU_ENCRYPT_KEY not set. Check your .env file.")
        sys.exit(1)

    challenge_value = "test_challenge_from_script_12345"

    # Feishu sends the plaintext challenge wrapped in the encrypted body
    plaintext_payload = {
        "challenge": challenge_value,
        "token": VERIFICATION_TOKEN,
        "type": "url_verification",
    }
    encrypted = encrypt_payload(plaintext_payload, ENCRYPT_KEY)

    # The actual HTTP body Feishu sends
    body_dict = {"encrypt": encrypted}
    body_bytes = json.dumps(body_dict).encode("utf-8")

    # Build signature headers (Feishu signs the *outer* body, not the plaintext)
    timestamp = str(int(time.time()))
    nonce = "testnonce123"
    sig = make_signature(timestamp, nonce, VERIFICATION_TOKEN, body_bytes)

    headers = {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": sig,
    }

    print(f"POST {SERVER_URL}/webhook")
    print(f"  Encrypted challenge payload (first 60 chars): {encrypted[:60]}...")
    print()

    resp = httpx.post(f"{SERVER_URL}/webhook", content=body_bytes, headers=headers)

    print(f"Response status : {resp.status_code}")
    print(f"Response body   : {resp.text}")
    print()

    if resp.status_code == 200:
        data = resp.json()
        if data.get("challenge") == challenge_value:
            print("✅ PASS — Server correctly returned the challenge value!")
        else:
            print(f"❌ FAIL — Expected challenge='{challenge_value}', got: {data}")
            sys.exit(1)
    else:
        print(f"❌ FAIL — Server returned HTTP {resp.status_code}")
        sys.exit(1)


if __name__ == "__main__":
    run_test()
