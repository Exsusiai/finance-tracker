"""P1-4: CEX (centralized exchange) spot balance providers.

Mirrors ``services/crypto_sync`` for off-chain accounts. Each provider
takes raw API credentials (api_key / api_secret / passphrase) — the
*caller* is responsible for decrypting them from
``exchange_connections.api_*_enc`` via ``services/bank_sync/crypto.py``
before invocation. This split keeps the encryption boundary at the
service edge (A4) and lets providers stay stateless / testable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.crypto_sync import BalanceItem


class ExchangeAuthError(RuntimeError):
    """Raised when an exchange returns an auth-failure response (HTTP 401 /
    business-error code).

    The router maps this to a 502 with a redacted message so we don't leak
    upstream specifics back to the client.
    """


@runtime_checkable
class ExchangeProvider(Protocol):
    exchange_id: str

    async def fetch_balances(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
    ) -> list[BalanceItem]:
        ...


def dispatch(exchange: str) -> ExchangeProvider:
    """Map an exchange id ('binance' / 'bitget') to its provider instance."""
    key = exchange.strip().lower()
    if key == "binance":
        from app.services.exchange_sync.binance import BinanceProvider

        return BinanceProvider()
    if key == "bitget":
        from app.services.exchange_sync.bitget import BitgetProvider

        return BitgetProvider()
    raise ValueError(f"Unsupported exchange {exchange!r}.")


__all__ = ["BalanceItem", "ExchangeAuthError", "ExchangeProvider", "dispatch"]
