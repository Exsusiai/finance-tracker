"""Bitget balance provider — supports both Classic and Unified accounts.

**Unified Account (UTA, 统一账户)** — Bitget's newer single-account mode. Once a
user upgrades, the classic v2 endpoints reject every call with
``code 40085: "You are in Unified Account mode, and the Classic Account API is
not supported"`` → holdings would silently read 0. So we probe the classic spot
endpoint first; on ``40085`` we switch to the v3 unified endpoints (same HMAC
signing) and sum BOTH wallets a unified account is split into:
``/api/v3/account/assets`` (trading) + ``/api/v3/account/funding-assets``
(funding / 资金). Per coin we take ``balance`` == ``available + locked/frozen``
(same "physical coin in wallet" semantics as the classic path).

**Classic account** — unchanged: aggregate four v2 endpoints by coin symbol:
- ``/api/v2/spot/account/assets``                            — spot wallet
- ``/api/v2/mix/account/accounts?productType=USDT-FUTURES``  — USDT-M
- ``/api/v2/mix/account/accounts?productType=USDC-FUTURES``  — USDC-M
- ``/api/v2/mix/account/accounts?productType=COIN-FUTURES``  — coin-margined

The sum is ``available + locked`` (and ``frozen`` for spot) per coin — i.e. the
coin physically in the wallet. ``equity`` (= balance + unrealizedPL) is
intentionally NOT used, so the total doesn't flicker with open-position PnL.

Per-endpoint failures (rate-limit / 5xx) skip that endpoint but don't kill the
rest — a Bitget mix-endpoint outage shouldn't drop the user's spot balances.
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
_V3_ASSETS_PATH = "/api/v3/account/assets"  # Unified Account (UTA) — trading
_V3_FUNDING_PATH = "/api/v3/account/funding-assets"  # UTA — funding (资金) wallet
# Product types we pull. Order matters only for deterministic test
# fixture iteration.
_MIX_PRODUCT_TYPES = ("USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES")
# Returned by every classic v2 endpoint once the account is upgraded to
# Unified Account mode → our cue to switch to the v3 unified endpoint.
_UNIFIED_ACCOUNT_CODE = "40085"


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

            # ─── Spot (also probes for Unified Account mode) ──────────
            try:
                spot_body = await self._get_raw(
                    http, _SPOT_PATH, query="",
                    api_key=api_key, api_secret=api_secret, passphrase=passphrase,
                )
                spot_code = str(spot_body.get("code", "00000"))
                if spot_code == _UNIFIED_ACCOUNT_CODE:
                    # Unified Account: classic endpoints are dead — switch to v3.
                    return await self._fetch_unified(
                        http, api_key=api_key, api_secret=api_secret, passphrase=passphrase,
                    )
                if spot_code != "00000":
                    raise ExchangeAuthError(
                        f"Bitget error {spot_code}: {spot_body.get('msg') or 'unknown'}"
                    )
                for row in spot_body.get("data") or []:
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

    async def _fetch_unified(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ) -> list[BalanceItem]:
        """Unified Account (UTA) path. A unified account splits funds into two
        wallets, BOTH queried via v3 and summed per coin:

        - ``/api/v3/account/assets``         — the unified TRADING account
        - ``/api/v3/account/funding-assets`` — the FUNDING (资金) wallet

        Per coin we take ``balance`` (== available + locked/frozen, the physical
        coin held); ``equity`` adds unrealizedPL, which we skip to keep net
        worth from flickering — same rule as the classic path. A funding-wallet
        failure doesn't drop the trading balances we already have."""
        totals: dict[str, Decimal] = {}

        # ─── Trading account (mandatory; auth/other errors are fatal) ─────
        body = await self._get_raw(
            http, _V3_ASSETS_PATH, query="",
            api_key=api_key, api_secret=api_secret, passphrase=passphrase,
        )
        code = str(body.get("code", "00000"))
        if code != "00000":
            raise ExchangeAuthError(
                f"Bitget v3 error {code}: {body.get('msg') or 'unknown'}"
            )
        data = body.get("data") or {}
        for a in (data.get("assets") if isinstance(data, dict) else None) or []:
            coin = a.get("coin")
            if not coin:
                continue
            qty = _dec(a.get("balance"))
            if qty == 0:
                qty = _dec(a.get("available")) + _dec(a.get("locked"))
            if qty > 0:
                totals[coin] = totals.get(coin, Decimal(0)) + qty

        # ─── Funding wallet (best-effort; don't lose trading on its failure) ─
        try:
            fbody = await self._get_raw(
                http, _V3_FUNDING_PATH, query="",
                api_key=api_key, api_secret=api_secret, passphrase=passphrase,
            )
            fcode = str(fbody.get("code", "00000"))
            if fcode == "00000":
                for a in fbody.get("data") or []:
                    coin = a.get("coin")
                    if not coin:
                        continue
                    qty = _dec(a.get("balance"))
                    if qty == 0:
                        qty = _dec(a.get("available")) + _dec(a.get("frozen"))
                    if qty > 0:
                        totals[coin] = totals.get(coin, Decimal(0)) + qty
            else:
                log.warning("bitget_funding_failed", code=fcode, msg=fbody.get("msg"))
        except httpx.HTTPError as exc:
            log.warning("bitget_funding_failed", error=str(exc))

        return [
            BalanceItem(symbol=coin, contract=None, quantity=qty.normalize(), decimals=8)
            for coin, qty in totals.items()
        ]

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
        """Signed GET that enforces ``code == 00000`` and returns ``data``.

        Non-zero business codes raise ``ExchangeAuthError`` so the caller flips
        ``last_sync_status`` to error (we can't reliably tell auth vs other
        business errors apart, and the row is unusable either way)."""
        body = await self._get_raw(
            http, path, query=query,
            api_key=api_key, api_secret=api_secret, passphrase=passphrase,
        )
        code = str(body.get("code", "00000"))
        if code != "00000":
            raise ExchangeAuthError(f"Bitget error {code}: {body.get('msg') or 'unknown'}")
        return body.get("data") or []

    async def _get_raw(
        self,
        http: httpx.AsyncClient,
        path: str,
        *,
        query: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ) -> dict:
        """Signed GET returning the full parsed body (does NOT enforce
        ``code``, so callers can branch on business codes like 40085). Raises
        only on HTTP-level auth/transport errors. ``query`` is URL-encoded
        WITHOUT the leading ``?`` (the sign payload includes it after the path)."""
        ts_ms = str(int(time.time() * 1000))
        # Per Bitget docs (v2 and v3 share the scheme): ACCESS-SIGN = base64(
        #   HMAC_SHA256(timestamp + METHOD + requestPath + queryString + body))
        # where requestPath = path WITHOUT query, queryString = "?" + query
        # (empty string when no query).
        sign_path = path + (f"?{query}" if query else "")
        sig = bitget_signature(ts_ms, "GET", sign_path, "", api_secret)
        resp = await http.get(
            sign_path,
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
        # Unified-account rejection comes back as HTTP 400 with a JSON body
        # (code 40085); let the caller inspect it rather than raising here.
        if resp.status_code == 400:
            try:
                return resp.json()
            except ValueError:
                pass
        resp.raise_for_status()
        return resp.json()


def _dec(value) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))
