"""AES-256-GCM encryption/decryption for sensitive bank connection credentials."""

from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key() -> bytes:
    """Get encryption key from runtime config.

    Reads from Settings (pydantic-settings, which loads .env at startup)
    first, then falls back to os.environ for the test path where the env
    var is set directly without going through Settings. The previous
    implementation read os.environ only — that breaks for the production
    flow because pydantic-settings populates Settings without writing
    back to os.environ.
    """
    key_hex = ""
    # Prefer Settings so a value in .env is honoured.
    try:
        from app.core.config import get_settings
        key_hex = get_settings().finance_bank_encryption_key or ""
    except Exception:
        pass
    if not key_hex:
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


def encrypt_str(value: str) -> str:
    """Encrypt a single string to a base64 blob.

    Format: base64(nonce[12] + ciphertext + tag[16]). Each call uses a
    fresh nonce so the same plaintext does NOT produce stable ciphertext
    (defence in depth — leaking that two columns hold the same value is
    useless to an attacker).
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, value.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_str(encrypted: str) -> str:
    """Inverse of :func:`encrypt_str`."""
    key = _get_key()
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def can_decrypt(encrypted: str | None) -> bool:
    """Return True iff *encrypted* can be decrypted with the CURRENT key.

    Used by the startup health-check and the settings endpoints to tell
    "no credential set" apart from "credential set but the encryption key
    changed, so it's now undecryptable" — the latter is the silent-failure
    mode that made LLM classification / CEX sync stop working without any
    visible error (see .learnings ERR-20260607-001). Empty / None input
    returns False (nothing to decrypt). Never raises.
    """
    if not encrypted:
        return False
    try:
        decrypt_str(encrypted)
        return True
    except Exception:
        return False
