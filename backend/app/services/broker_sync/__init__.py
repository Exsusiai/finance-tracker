"""Brokerage position sync (read-only).

Mirrors ``services/exchange_sync`` for traditional brokers. The first (and
currently only) provider is Interactive Brokers via the Flex Web Service —
a token-based **reporting** API available on every IBKR account type
(including Lite; no Pro / funding requirement, since it isn't a trading
API).

Each provider takes its credentials (a Flex token + query id for IBKR) and
returns a list of ``BrokerPosition``. The *caller* (orchestrator) decrypts
the token from ``broker_connections.token_enc`` before invocation, keeping
the encryption boundary at the service edge — same split as the CEX path.

Design notes:
- Flex statements are end-of-day snapshots. ``markPrice`` / ``currency``
  come straight from the statement, so the upsert layer writes prices
  directly (no CoinGecko / yfinance round-trip needed).
- Asset class is mapped from IBKR's ``assetCategory`` + the position
  currency onto the project's closed ``assets.asset_class`` CHECK set.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


class BrokerSyncError(RuntimeError):
    """Raised when a broker reporting API returns a hard failure (bad token,
    invalid query, IP restriction, …).

    The router maps this to a 502 with a redacted message so upstream
    specifics (and the token) never leak back to the client.
    """


@dataclass(frozen=True)
class BrokerPosition:
    """One open position line from a brokerage statement.

    - ``symbol`` is the ticker as reported by the broker (e.g. ``AAPL``).
    - ``conid`` is IBKR's stable contract id — a unique, exchange-agnostic
      identifier that disambiguates dual-listed / renamed tickers. Stored
      on ``Asset.data_source_id``.
    - ``quantity`` is the human-readable share/unit count (already signed:
      negative for shorts).
    - ``mark_price`` / ``currency`` are the per-unit market price and its
      currency, taken straight from the statement.
    - ``avg_cost`` is the per-unit cost basis (``costBasisPrice``), in the
      same ``currency``. ``None`` when the broker omits it.
    - ``asset_category`` is the broker's raw category code (``STK`` /
      ``FUND`` / ``BOND`` / …) — mapped to ``asset_class`` by
      :func:`map_asset_class`.
    """

    symbol: str
    conid: str | None
    asset_category: str
    currency: str
    quantity: Decimal
    mark_price: Decimal | None
    avg_cost: Decimal | None
    description: str | None
    # Pre-resolved asset class. IBKR leaves this None (the upsert derives it
    # from assetCategory + currency via map_asset_class). Trade Republic sets
    # it directly from the ISIN, since TR's portfolio feed carries no
    # IBKR-style category code.
    asset_class: str | None = None


@runtime_checkable
class BrokerProvider(Protocol):
    """Structural type for one broker's position provider."""

    provider_id: str

    async def fetch_positions(self) -> list[BrokerPosition]:
        ...


# ─── Asset-class mapping ─────────────────────────────────────────────────────

# IBKR reports STK for common shares; we split it by quote currency to land
# on the project's regional stock classes. Everything else maps to the
# closest member of the closed ``ck_asset_class`` CHECK set.
_STOCK_CLASS_BY_CURRENCY: dict[str, str] = {
    "USD": "us_stock",
    "EUR": "eu_stock",
    "CNY": "a_share",
    "HKD": "eu_stock",  # no HK bucket — group with eu_stock as "other regional"
}


def map_asset_class(asset_category: str, currency: str) -> str:
    """Map an IBKR ``assetCategory`` + currency onto ``assets.asset_class``.

    The target set is closed (DB CHECK): cash / a_share / eu_stock /
    us_stock / crypto / gold / bond / fund / other. Unknown categories
    fall back to ``other`` rather than raising — a new IBKR category must
    never break a sync.
    """
    cat = (asset_category or "").strip().upper()
    cur = (currency or "").strip().upper()
    if cat == "STK":
        return _STOCK_CLASS_BY_CURRENCY.get(cur, "us_stock")
    if cat in ("FUND", "ETF", "FXF"):
        return "fund"
    if cat in ("BOND", "BILL", "NOTE"):
        return "bond"
    if cat in ("CASH", "CFD"):
        return "cash"
    if cat in ("CMDTY", "METAL"):
        return "gold"
    return "other"


# ─── Dispatcher ──────────────────────────────────────────────────────────────


def dispatch(provider: str, *, token: str, query_id: str) -> BrokerProvider:
    """Return the right provider for a broker id.

    Lazy-imports so the optional ``ibflex`` dependency is only required when
    an IBKR account is actually synced.
    """
    key = provider.strip().lower()
    if key == "ibkr":
        from app.services.broker_sync.ibkr import IBKRFlexProvider

        return IBKRFlexProvider(token=token, query_id=query_id)
    if key == "traderepublic":
        from app.services.broker_sync.traderepublic import TradeRepublicProvider

        # For Trade Republic the "token" is the serialized web-login cookies.
        return TradeRepublicProvider(cookies_blob=token)
    raise ValueError(f"Unsupported broker provider {provider!r}.")


__all__ = [
    "BrokerPosition",
    "BrokerProvider",
    "BrokerSyncError",
    "dispatch",
    "map_asset_class",
]
