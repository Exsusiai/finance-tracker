"""Trading 212 public API provider (read-only).

Official REST API (beta) — https://docs.trading212.com/api. Available on
Invest / Stocks ISA accounts (one API key pair per account; CFD not
supported). Credentials are an API Key (ID) + API Secret generated in the
T212 app under Settings → API; grant only the read scopes (portfolio +
account data). Auth is HTTP Basic with key as username, secret as password.

Two GETs per sync:
  1. ``/api/v0/equity/positions`` (1 req/1s) — open positions with the
     instrument (ticker / name / isin / currency) and ``currentPrice``
     embedded, so no metadata round-trip or external price source is needed.
  2. ``/api/v0/equity/account/summary`` (1 req/5s) — idle cash in the
     account currency. Failure here is non-fatal: positions still sync.

Credential blob shape (decrypted ``broker_connections.token_enc``):
``"<api_key>:<api_secret>"`` — the same join Basic auth itself performs, so
a colon can never appear inside the key part.

Note: the positions payload carries no instrument *type*, so ETFs land in
the regional stock classes (us_stock / eu_stock) rather than ``fund`` —
distinguishing them would need the ~5 MB ``metadata/instruments`` call
(1 req/50s), deliberately skipped.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx
import structlog

from app.services.broker_sync import BrokerPosition, BrokerSyncError

log = structlog.get_logger(__name__)

_LIVE_BASE = "https://live.trading212.com/api/v0"

_HEADERS = {"user-agent": "finance-tracker/1.0"}

_STATUS_HINTS: dict[int, str] = {
    401: (
        "Trading 212: invalid API key/secret, or LIVE/DEMO environment "
        "mismatch (keys only work in the environment they were created in)."
    ),
    403: (
        "Trading 212: API key lacks a required read permission — enable "
        "account data + portfolio read scopes when generating the key."
    ),
    429: "Trading 212: rate limited — try again in a minute.",
}


# ─── ticker / asset-class mapping ────────────────────────────────────────────

# T212 tickers are "{SYMBOL}_{EXCHANGE}_{TYPE}" (AAPL_US_EQ, VUSA_LSE_EQ)
# or "{SYMBOL}_CRYPTO" (BTC_CRYPTO); some have no exchange segment (VUAGl_EQ).


def _clean_symbol(ticker: str) -> str:
    """Strip the exchange/type suffixes off a T212 ticker → display symbol."""
    parts = (ticker or "").strip().upper().split("_")
    if len(parts) >= 2 and parts[-1] in ("EQ", "CRYPTO"):
        parts = parts[:-1]
        # Drop a trailing exchange code (US / LSE / …) when one is present.
        if len(parts) >= 2 and 2 <= len(parts[-1]) <= 4:
            parts = parts[:-1]
    return "_".join(parts)


def _t212_asset_class(ticker: str, isin: str) -> str:
    """Positions carry no instrument type — derive the class from the ticker
    shape (crypto suffix / US exchange segment) and the ISIN country prefix."""
    t = (ticker or "").strip().upper()
    i = (isin or "").strip().upper()
    if t.endswith("_CRYPTO"):
        return "crypto"
    if i.startswith("US") or "_US_" in t:
        return "us_stock"
    if i:
        return "eu_stock"
    return "other"


# ─── provider ────────────────────────────────────────────────────────────────


class Trading212Provider:
    """Fetch open positions + idle cash from the Trading 212 public API.

    ``client`` is injectable so tests drive everything through
    ``httpx.MockTransport`` with zero network.
    """

    provider_id = "trading212"

    def __init__(
        self,
        *,
        credentials_blob: str,
        client: httpx.AsyncClient | None = None,
        base_url: str = _LIVE_BASE,
    ) -> None:
        key, sep, secret = (credentials_blob or "").partition(":")
        if not sep or not key.strip() or not secret.strip():
            raise BrokerSyncError(
                "Trading 212 credentials are malformed — re-enter the "
                "API key and secret."
            )
        self._auth = (key.strip(), secret.strip())
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def fetch_positions(self) -> list[BrokerPosition]:
        if self._client is not None:
            return await self._run(self._client)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._run(client)

    async def _run(self, client: httpx.AsyncClient) -> list[BrokerPosition]:
        raw_positions = await self._get_json(client, "/equity/positions")
        if not isinstance(raw_positions, list):
            raise BrokerSyncError("Trading 212: unexpected positions payload.")
        out: list[BrokerPosition] = []
        for raw in raw_positions:
            pos = _map_position(raw)
            if pos is not None:
                out.append(pos)

        # Idle cash — non-fatal: a summary hiccup must not block holdings.
        try:
            summary = await self._get_json(client, "/equity/account/summary")
            cash = _cash_from_summary(summary)
            if cash is not None:
                out.append(cash)
        except BrokerSyncError as exc:
            log.warning("t212_summary_failed", error=str(exc))
        return out

    async def _get_json(self, client: httpx.AsyncClient, path: str):
        try:
            resp = await client.get(
                f"{self._base_url}{path}", auth=self._auth, headers=_HEADERS
            )
        except httpx.HTTPError as exc:
            raise BrokerSyncError(
                f"Trading 212: network error ({exc.__class__.__name__})."
            ) from exc
        if resp.status_code != 200:
            hint = _STATUS_HINTS.get(resp.status_code)
            raise BrokerSyncError(hint or f"Trading 212: HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerSyncError("Trading 212: response is not valid JSON.") from exc


# ─── payload mapping ─────────────────────────────────────────────────────────


def _map_position(raw: dict) -> BrokerPosition | None:
    if not isinstance(raw, dict):
        return None
    instrument = raw.get("instrument") or {}
    ticker = (instrument.get("ticker") or raw.get("ticker") or "").strip()
    symbol = _clean_symbol(ticker)
    quantity = _opt_decimal(raw.get("quantity"))
    if not symbol or quantity is None:
        return None
    isin = (instrument.get("isin") or "").strip().upper()
    currency = (instrument.get("currency") or "").strip().upper() or "USD"
    return BrokerPosition(
        symbol=symbol,
        # ISIN is the stable cross-provider id (matches the TR convention);
        # crypto instruments have none → the full T212 ticker still uniquely
        # identifies them.
        conid=isin or ticker,
        asset_category="",
        currency=currency,
        quantity=quantity,
        mark_price=_opt_decimal(raw.get("currentPrice")),
        avg_cost=_opt_decimal(raw.get("averagePricePaid")),
        description=(instrument.get("name") or "").strip() or None,
        asset_class=_t212_asset_class(ticker, isin),
    )


def _cash_from_summary(summary) -> BrokerPosition | None:
    """availableToTrade + reservedForOrders + inPies → one cash position in
    the account currency (mirrors the IBKR CashReport handling)."""
    if not isinstance(summary, dict):
        return None
    currency = (summary.get("currency") or "").strip().upper()
    cash_obj = summary.get("cash") or {}
    if not currency or not isinstance(cash_obj, dict):
        return None
    total = Decimal("0")
    for field in ("availableToTrade", "reservedForOrders", "inPies"):
        total += _opt_decimal(cash_obj.get(field)) or Decimal("0")
    if total == 0:
        return None
    return BrokerPosition(
        symbol=currency,
        conid=None,
        asset_category="cash",
        currency=currency,
        quantity=total,
        mark_price=Decimal("1"),
        avg_cost=None,
        description=f"{currency} 现金",
        asset_class="cash",
    )


def _opt_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
