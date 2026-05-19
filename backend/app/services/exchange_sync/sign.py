"""HMAC signing helpers for CEX REST APIs.

Kept as pure functions so they can be unit-tested with known vectors
without spinning up an HTTP client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def binance_signature(query_string: str, api_secret: str) -> str:
    """Binance: HMAC-SHA256 hex digest of the query string."""
    return hmac.new(
        api_secret.encode(), query_string.encode(), hashlib.sha256
    ).hexdigest()


def bitget_signature(
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
    api_secret: str,
) -> str:
    """Bitget v2: base64(HMAC-SHA256(timestamp + METHOD + path + body)).

    The method MUST be upper-case in the pre-image; passing 'get' silently
    produces an invalid signature (Bitget then returns ``40006 Invalid
    sign``). We normalise here so callers can't fall into that trap.
    """
    payload = (timestamp + method.upper() + request_path + (body or "")).encode()
    digest = hmac.new(api_secret.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()
