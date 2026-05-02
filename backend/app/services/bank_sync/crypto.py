"""AES-256-GCM encryption/decryption for sensitive bank connection credentials."""

from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key() -> bytes:
    """Get encryption key from environment variable.

    The key must be a 32-byte (64 hex chars) string stored in
    FINANCE_BANK_ENCRYPTION_KEY environment variable.
    """
    key_hex = os.environ.get("FINANCE_BANK_ENCRYPTION_KEY", "")
    if not key_hex:
        raise RuntimeError(
            "FINANCE_BANK_ENCRYPTION_KEY not set. "
            "Generate one with: python -c \"import os; print(os.urandom(32).hex())\""
        )
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise ValueError("FINANCE_BANK_ENCRYPTION_KEY must be 32 bytes (64 hex chars)")
    return key


def encrypt_credentials(data: dict[str, Any]) -> str:
    """Encrypt a credentials dict to a base64 string.

    Format: base64(nonce[12] + ciphertext + tag[16])
    """
    import json

    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_credentials(encrypted: str) -> dict[str, Any]:
    """Decrypt a base64-encoded credentials string back to a dict."""
    import json

    key = _get_key()
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
