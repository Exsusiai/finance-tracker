"""Bitget v2 spot + futures balance provider.

Hits four endpoints per sync and aggregates by coin symbol:
- ``/api/v2/spot/account/assets``                            — spot wallet
- ``/api/v2/mix/account/accounts?productType=USDT-FUTURES``  — USDT-M
- ``/api/v2/mix/account/accounts?productType=USDC-FUTURES``  — USDC-M
- ``/api/v2/mix/account/accounts?productType=COIN-FUTURES``  — coin-margined

The sum is ``available + locked`` (and ``frozen`` for spot) per coin —
i.e. the coin balance physically sitting in the wallet. ``equity`` =
``available + locked + unrealizedPL`` is intentionally NOT used, so the
account total doesn't flicker with PnL on open positions. Open-position
notional value is also not tracked.

Per-endpoint failures (rate-limit / 5xx) skip that endpoint but don't
kill the rest — a Bitget mix-endpoint outage shouldn't drop the user's
spot balances.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import ClassVar

import httpx
import structlog

from app.services.crypto_sync import BalanceItem
from app.services.exchange_sync import ExchangeAuthError
from app.services.exchange_sync.sign import bitget_signature

log = structlog.get_logger(__name__)

_BASE = "https://api.bitget.com"
_SPOT_PATH = "/api/v2/spot/account/assets"
_MIX_PATH = "/api/v2/mix/account/accounts"
# Product types we pull. Order matters only for deterministic test
# fixture iteration.
_MIX_PRODUCT_TYPES = ("USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES")


class BitgetProvider:
    exchange_id: ClassVar[str] = "bitget"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def fetch_balances(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
    ) -> list[BalanceItem]:
        if not api_key or not api_secret:
            raise ValueError("api_key / api_secret are required.")
        if not passphrase:
            # Bitget *always* requires a passphrase — fail before the wire
            # so the user gets a clean UI error instead of upstream
            # "Invalid sign".
            raise ValueError("Bitget requires a passphrase (set at API-key creation).")

        owns = self._http is None
        http = self._http or httpx.AsyncClient(base_url=_BASE, timeout=20.0)
        try:
            # Aggregate by coin: {coin → quantity} across all endpoints.
            totals: dict[str, Decimal] = {}

            # ─── Spot ─────────────────────────────────────────────────
            try:
                spot_data = await self._get(
                    http, _SPOT_PATH, query="",
                    api_key=api_key, api_secret=api_secret, passphrase=passphrase,
                )
                for row in spot_data:
                    coin = row.get("coin")
                    if not coin:
                        continue
                    qty = (
                        _dec(row.get("available"))
                        + _dec(row.get("frozen"))
                        + _dec(row.get("locked"))
                    )
                    if qty > 0:
                        totals[coin] = totals.get(coin, Decimal(0)) + qty
            except ExchangeAuthError:
                # Auth fails here → can't trust anything that follows; bail.
                raise
            except httpx.HTTPError as exc:
                log.warning("bitget_spot_failed", error=str(exc))
                # Continue with futures — spot may be temporarily 5xx.

            # ─── Futures (3 product types) ───────────────────────────
            for product in _MIX_PRODUCT_TYPES:
                try:
                    qs = f"productType={product}"
                    mix_data = await self._get(
                        http, _MIX_PATH, query=qs,
                        api_key=api_key, api_secret=api_secret, passphrase=passphrase,
                    )
                    for row in mix_data:
                        coin = row.get("marginCoin")
                        if not coin:
                            continue
                        # available + locked = physical coin balance.
                        # Excludes unrealizedPL so net worth doesn't
                        # flicker with open-position PnL.
                        qty = _dec(row.get("available")) + _dec(row.get("locked"))
                        if qty > 0:
                            totals[coin] = totals.get(coin, Decimal(0)) + qty
                except httpx.HTTPError as exc:
                    log.warning(
                        "bitget_mix_failed", product_type=product, error=str(exc),
                    )
                    continue

            return [
                BalanceItem(
                    symbol=coin,
                    contract=None,
                    quantity=qty.normalize(),
                    decimals=8,
                )
                for coin, qty in totals.items()
            ]
        finally:
            if owns:
                await http.aclose()

    async def _get(
        self,
        http: httpx.AsyncClient,
        path: str,
        *,
        query: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ) -> list[dict]:
        """Signed GET. ``query`` is the URL-encoded query string WITHOUT
        the leading ``?`` (Bitget's sign payload includes it after the
        path)."""
        ts_ms = str(int(time.time() * 1000))
        # Per Bitget v2 docs: ACCESS-SIGN = base64(HMAC_SHA256(
        #   timestamp + METHOD + requestPath + queryString + body))
        # where requestPath = path WITHOUT query, queryString = "?" + query
        # (empty string when no query).
        sign_path = path + (f"?{query}" if query else "")
        sig = bitget_signature(ts_ms, "GET", sign_path, "", api_secret)
        url = sign_path
        resp = await http.get(
            url,
            headers={
                "ACCESS-KEY": api_key,
                "ACCESS-SIGN": sig,
                "ACCESS-TIMESTAMP": ts_ms,
                "ACCESS-PASSPHRASE": passphrase,
                "Content-Type": "application/json",
                "locale": "en-US",
            },
        )
        if resp.status_code in (401, 403):
            raise ExchangeAuthError(
                f"Bitget rejected credentials (HTTP {resp.status_code})."
            )
        resp.raise_for_status()
        body = resp.json()
        code = str(body.get("code", "00000"))
        if code != "00000":
            msg = body.get("msg") or "unknown"
            # Auth-like errors → bail loudly so the caller flips
            # last_sync_status to error. Other business errors are
            # bubbled the same way since we can't differentiate
            # reliably and the row is unusable anyway.
            raise ExchangeAuthError(f"Bitget error {code}: {msg}")
        return body.get("data") or []


def _dec(value) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))
