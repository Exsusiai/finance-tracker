"""Binance spot balance provider (GET /api/v3/account)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import ClassVar
from urllib.parse import urlencode

import httpx

from app.services.crypto_sync import BalanceItem
from app.services.exchange_sync import ExchangeAuthError
from app.services.exchange_sync.sign import binance_signature

_BASE = "https://api.binance.com"
_RECV_WINDOW_MS = 5_000


class BinanceProvider:
    exchange_id: ClassVar[str] = "binance"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def fetch_balances(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,  # unused on Binance — kept for Protocol
    ) -> list[BalanceItem]:
        if not api_key or not api_secret:
            raise ValueError("api_key / api_secret are required.")

        owns = self._http is None
        http = self._http or httpx.AsyncClient(base_url=_BASE, timeout=20.0)
        try:
            ts_ms = int(time.time() * 1000)
            params = {"timestamp": ts_ms, "recvWindow": _RECV_WINDOW_MS}
            qs = urlencode(params)
            sig = binance_signature(qs, api_secret)
            resp = await http.get(
                f"/api/v3/account?{qs}&signature={sig}",
                headers={"X-MBX-APIKEY": api_key},
            )
            if resp.status_code in (401, 403):
                raise ExchangeAuthError(
                    f"Binance rejected credentials (HTTP {resp.status_code})."
                )
            resp.raise_for_status()
            data = resp.json()
            return self._parse(data.get("balances", []))
        finally:
            if owns:
                await http.aclose()

    @staticmethod
    def _parse(rows: list[dict]) -> list[BalanceItem]:
        out: list[BalanceItem] = []
        for row in rows:
            free = Decimal(row.get("free", "0"))
            locked = Decimal(row.get("locked", "0"))
            total = free + locked
            if total <= 0:
                continue
            out.append(
                BalanceItem(
                    symbol=row["asset"],
                    contract=None,  # off-chain
                    quantity=total.normalize(),
                    decimals=8,  # Binance reports 8-dp strings uniformly
                )
            )
        return out
