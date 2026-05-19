"""P1-4: on-chain wallet balance providers.

Each chain is fronted by a single provider that returns a list of
``BalanceItem`` for a given wallet address. The orchestration layer
(``services/crypto_sync/sync.py`` — landing in A4) loops over the
``chain_addresses`` rows of an account and aggregates results.

Design mirrors ``services/llm/`` — a small Protocol so a future provider
swap (Moralis instead of Alchemy, mempool.space instead of Blockstream)
plugs in without touching call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class BalanceItem:
    """One on-chain holding line.

    - ``symbol`` is set when the provider can identify it (native asset
      always; for ERC-20 / TRC-20 via on-chain metadata). For SPL tokens
      we leave it ``None`` and let the upstream symbol-resolution layer
      (CoinGecko by contract address) fill it.
    - ``contract`` is ``None`` for the native asset of a chain, otherwise
      the token contract / mint address.
    - ``quantity`` is the **human-readable** amount (already divided by
      ``10**decimals``).
    - ``decimals`` is retained so callers can re-derive the integer
      base-unit amount when needed for cost-basis math.
    """

    symbol: Optional[str]
    contract: Optional[str]
    quantity: Decimal
    decimals: int


@runtime_checkable
class CryptoChainProvider(Protocol):
    """Structural type for one-chain providers."""

    chain_id: str  # human chain id, e.g. "ethereum"

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        ...


# ─── Dispatcher ─────────────────────────────────────────────────────────────


# EVM L1 + L2 chains all served by Alchemy.
_EVM_CHAINS: frozenset[str] = frozenset(
    {
        "ethereum",
        "arbitrum",
        "optimism",
        "base",
        "polygon",
        "polygon-zkevm",
        "zksync",
        "linea",
        "scroll",
        "mantle",
        "blast",
    }
)


def dispatch(chain: str, alchemy_api_key: str | None) -> CryptoChainProvider:
    """Return the right provider for the given chain id.

    Lazy-imports so the test file can collect even if optional deps land
    later. ``alchemy_api_key`` is only required for EVM chains; everything
    else uses public endpoints.
    """

    chain = chain.strip().lower()
    if chain in _EVM_CHAINS:
        if not alchemy_api_key:
            raise ValueError(
                f"alchemy_api_key is required for EVM chain {chain!r}; "
                "set ALCHEMY_API_KEY in .env."
            )
        from app.services.crypto_sync.evm_alchemy import AlchemyEVMProvider

        return AlchemyEVMProvider(chain=chain, api_key=alchemy_api_key)
    if chain == "bitcoin":
        from app.services.crypto_sync.btc_blockstream import BlockstreamProvider

        return BlockstreamProvider()
    if chain == "solana":
        from app.services.crypto_sync.sol_rpc import SolanaRPCProvider

        return SolanaRPCProvider()
    if chain == "tron":
        from app.services.crypto_sync.tron_grid import TronGridProvider

        return TronGridProvider()
    raise ValueError(f"Unsupported chain {chain!r} — no provider wired in A2.")


__all__ = ["BalanceItem", "CryptoChainProvider", "dispatch"]
