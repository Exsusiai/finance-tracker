"""TronGrid provider for Tron mainnet (TRX + TRC-20). No key required."""

from __future__ import annotations

from decimal import Decimal

import httpx

from app.services.crypto_sync import BalanceItem

_BASE = "https://api.trongrid.io"
_SUN_PER_TRX = Decimal("1000000")


class TronGridProvider:
    chain_id = "tron"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        # Per-instance contract metadata cache so we don't refetch the
        # decimals/symbol for the same TRC-20 across multiple addresses
        # in a single sync round.
        self._meta_cache: dict[str, dict] = {}

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        owns = self._http is None
        http = self._http or httpx.AsyncClient(base_url=_BASE, timeout=20.0)
        try:
            acct = (await self._get(http, f"/v1/accounts/{address}"))["data"]
            if not acct:
                return []
            row = acct[0]
            items: list[BalanceItem] = []

            sun = int(row.get("balance", 0))
            if sun > 0:
                items.append(
                    BalanceItem(
                        symbol="TRX",
                        contract=None,
                        quantity=(Decimal(sun) / _SUN_PER_TRX).normalize(),
                        decimals=6,
                    )
                )

            # TRC-20 array shape: [{"<contract>": "<amount_str>"}, …]
            for entry in row.get("trc20", []) or []:
                for contract, amount_str in entry.items():
                    base = int(amount_str)
                    if base <= 0:
                        continue
                    meta = await self._token_meta(http, contract)
                    if meta is None:
                        continue
                    decimals = int(meta.get("decimals", 0))
                    qty = (
                        (Decimal(base) / (Decimal(10) ** decimals)).normalize()
                        if decimals > 0
                        else Decimal(base)
                    )
                    items.append(
                        BalanceItem(
                            symbol=meta.get("symbol"),
                            contract=contract,
                            quantity=qty,
                            decimals=decimals,
                        )
                    )
            return items
        finally:
            if owns:
                await http.aclose()

    async def _token_meta(self, http: httpx.AsyncClient, contract: str) -> dict | None:
        if contract in self._meta_cache:
            return self._meta_cache[contract]
        try:
            data = await self._get(http, f"/v1/contracts/{contract}")
        except httpx.HTTPStatusError:
            self._meta_cache[contract] = None  # type: ignore[assignment]
            return None
        rows = data.get("data") or []
        if not rows:
            self._meta_cache[contract] = None  # type: ignore[assignment]
            return None
        self._meta_cache[contract] = rows[0]
        return rows[0]

    @staticmethod
    async def _get(http: httpx.AsyncClient, path: str) -> dict:
        resp = await http.get(path)
        resp.raise_for_status()
        return resp.json()
