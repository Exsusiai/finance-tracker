"""Public Solana RPC provider — covers native SOL + SPL tokens.

Helius / QuickNode would offer richer metadata, but the bare-metal
``getBalance`` + ``getTokenAccountsByOwner`` JSON-RPC calls available on
``api.mainnet-beta.solana.com`` already give us everything needed for
holdings tracking. Symbol resolution by mint is upstream's job.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from app.services.crypto_sync import BalanceItem

_BASE = "https://api.mainnet-beta.solana.com"
_LAMPORTS_PER_SOL = Decimal("1000000000")
_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class SolanaRPCProvider:
    chain_id = "solana"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        owns = self._http is None
        http = self._http or httpx.AsyncClient(base_url=_BASE, timeout=20.0)
        try:
            items: list[BalanceItem] = []

            native_resp = await self._rpc(http, "getBalance", [address])
            lamports = int(native_resp["result"]["value"])
            if lamports > 0:
                items.append(
                    BalanceItem(
                        symbol="SOL",
                        contract=None,
                        quantity=(Decimal(lamports) / _LAMPORTS_PER_SOL).normalize(),
                        decimals=9,
                    )
                )

            spl_resp = await self._rpc(
                http,
                "getTokenAccountsByOwner",
                [
                    address,
                    {"programId": _SPL_TOKEN_PROGRAM},
                    {"encoding": "jsonParsed"},
                ],
            )
            for acct in spl_resp["result"]["value"]:
                info = acct["account"]["data"]["parsed"]["info"]
                ta = info["tokenAmount"]
                amount_str = ta.get("uiAmountString") or "0"
                qty = Decimal(amount_str)
                if qty <= 0:
                    continue
                items.append(
                    BalanceItem(
                        symbol=None,  # mint-only, symbol resolved upstream
                        contract=info["mint"],
                        quantity=qty.normalize(),
                        decimals=int(ta["decimals"]),
                    )
                )
            return items
        finally:
            if owns:
                await http.aclose()

    @staticmethod
    async def _rpc(http: httpx.AsyncClient, method: str, params: list) -> dict:
        resp = await http.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        return resp.json()
