"""Blockstream Esplora provider for Bitcoin (no API key required)."""

from __future__ import annotations

from decimal import Decimal

import httpx

from app.services.crypto_sync import BalanceItem

_BASE = "https://blockstream.info"
_SATS_PER_BTC = Decimal("100000000")


class BlockstreamProvider:
    chain_id = "bitcoin"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        owns = self._http is None
        http = self._http or httpx.AsyncClient(base_url=_BASE, timeout=20.0)
        try:
            resp = await http.get(f"/api/address/{address}")
            resp.raise_for_status()
            data = resp.json()
            # Confirmed-only balance — mempool not counted as a holding so
            # users don't see flicker on pending inbound tx.
            chain = data.get("chain_stats", {})
            funded = int(chain.get("funded_txo_sum", 0))
            spent = int(chain.get("spent_txo_sum", 0))
            sats = funded - spent
            if sats <= 0:
                return []
            qty = (Decimal(sats) / _SATS_PER_BTC).normalize()
            return [
                BalanceItem(
                    symbol="BTC",
                    contract=None,
                    quantity=qty,
                    decimals=8,
                )
            ]
        finally:
            if owns:
                await http.aclose()
