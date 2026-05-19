"""Alchemy provider — covers all EVM L1+L2 chains we support.

One ``alchemy_getTokenBalances`` call returns every ERC-20 the wallet has
ever interacted with (Alchemy's indexer is the whole reason we use them
instead of raw RPC). We follow up with one ``alchemy_getTokenMetadata``
per contract to learn decimals + symbol.

Native chain currency comes from a standard ``eth_getBalance`` call —
no Alchemy-specific magic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import httpx

from app.services.crypto_sync import BalanceItem


# Mapping chain id → Alchemy subdomain. Native asset symbol per chain.
_CHAINS: dict[str, tuple[str, str]] = {
    # chain_id           subdomain                                  native
    "ethereum":          ("https://eth-mainnet.g.alchemy.com",      "ETH"),
    "arbitrum":          ("https://arb-mainnet.g.alchemy.com",      "ETH"),
    "optimism":          ("https://opt-mainnet.g.alchemy.com",      "ETH"),
    "base":              ("https://base-mainnet.g.alchemy.com",     "ETH"),
    "polygon":           ("https://polygon-mainnet.g.alchemy.com",  "MATIC"),
    "polygon-zkevm":     ("https://polygonzkevm-mainnet.g.alchemy.com", "ETH"),
    "zksync":            ("https://zksync-mainnet.g.alchemy.com",   "ETH"),
    "linea":             ("https://linea-mainnet.g.alchemy.com",    "ETH"),
    "scroll":            ("https://scroll-mainnet.g.alchemy.com",   "ETH"),
    "mantle":            ("https://mantle-mainnet.g.alchemy.com",   "MNT"),
    "blast":             ("https://blast-mainnet.g.alchemy.com",    "ETH"),
}


class AlchemyEVMProvider:
    """One instance per (chain, api key) pair."""

    NATIVE_DECIMALS: ClassVar[int] = 18  # every supported EVM chain uses 18-dec native
    # Class-level placeholder so `hasattr(cls, 'chain_id')` succeeds for the
    # Protocol structural-check. Real value is set per-instance in __init__.
    chain_id: ClassVar[str] = ""

    def __init__(
        self,
        chain: str,
        api_key: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if chain not in _CHAINS:
            raise ValueError(
                f"Unsupported EVM chain {chain!r}. "
                f"Known: {sorted(_CHAINS)}"
            )
        if not api_key:
            raise ValueError("api_key is required.")
        self.chain_id = chain
        self._subdomain, self.native_symbol = _CHAINS[chain]
        self._api_key = api_key
        # `endpoint` is exposed for debugging / tests.
        self.endpoint = f"{self._subdomain}/v2/{api_key}"
        # The injected client is reused so tests can stub the transport.
        # In production we create a private one in `fetch_balances`.
        self._http = http

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        owns_client = self._http is None
        http = self._http or httpx.AsyncClient(base_url=self._subdomain, timeout=20.0)
        try:
            # 1) Native balance.
            native_hex: str = (
                await self._rpc(http, "eth_getBalance", [address, "latest"])
            )["result"]
            native_qty = self._scale(native_hex, self.NATIVE_DECIMALS)

            # 2) ERC-20 balances.
            tb = (
                await self._rpc(http, "alchemy_getTokenBalances", [address, "erc20"])
            )["result"]

            items: list[BalanceItem] = []
            if native_qty > 0:
                items.append(
                    BalanceItem(
                        symbol=self.native_symbol,
                        contract=None,
                        quantity=native_qty,
                        decimals=self.NATIVE_DECIMALS,
                    )
                )

            for entry in tb.get("tokenBalances", []):
                raw = entry.get("tokenBalance") or "0x0"
                if int(raw, 16) == 0:
                    continue
                contract = entry["contractAddress"]
                meta = (
                    await self._rpc(http, "alchemy_getTokenMetadata", [contract])
                )["result"]
                decimals = meta.get("decimals")
                if decimals is None:
                    # Unknown decimals → we can't faithfully scale; skip.
                    # Recording the raw integer would surface as a giant
                    # number in the UI which is worse than silently
                    # dropping. Logged upstream.
                    continue
                items.append(
                    BalanceItem(
                        symbol=meta.get("symbol"),
                        contract=contract,
                        quantity=self._scale(raw, decimals),
                        decimals=int(decimals),
                    )
                )
            return items
        finally:
            if owns_client:
                await http.aclose()

    # ─── helpers ──────────────────────────────────────────────────────

    async def _rpc(
        self, http: httpx.AsyncClient, method: str, params: list
    ) -> dict:
        resp = await http.post(
            self.endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _scale(hex_or_int: str | int, decimals: int) -> Decimal:
        """Convert a hex / int base-unit amount to a human Decimal."""
        if isinstance(hex_or_int, str):
            base = int(hex_or_int, 16)
        else:
            base = int(hex_or_int)
        if decimals <= 0:
            return Decimal(base)
        # Decimal scaling without float loss.
        scaled = Decimal(base) / (Decimal(10) ** decimals)
        # Normalize trailing zeros so "2.0" → "2".
        return scaled.normalize() if scaled != 0 else Decimal(0)
