"""CoinGecko price helpers for the wallet/exchange sync pipeline.

Two API shapes:
- ``/api/v3/simple/price?ids=<coin>&vs_currencies=usd`` — native chain
  coins by CoinGecko's own id ("ethereum", "bitcoin", …).
- ``/api/v3/simple/token_price/<platform>?contract_addresses=...&vs_currencies=usd``
  — ERC-20 / SPL / TRC-20 tokens by their on-chain contract / mint
  address. Multiple contracts may be batched as a comma-separated list.

We quote everything in USD on the wire (CoinGecko doesn't offer USDT as
a `vs_currency`) and treat USDT == USD == 1.0 in the price column,
since the rest of the project uses USDT as the crypto-account unit.

Network IO is via ``httpx.AsyncClient`` — tests inject a ``MockTransport``
to avoid hitting the live API.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import httpx
import structlog

log = structlog.get_logger(__name__)

# Internal chain id → CoinGecko platform id. Adding a new EVM chain here
# is usually the only change needed once Alchemy already covers it.
CHAIN_TO_PLATFORM: Final[dict[str, str]] = {
    "ethereum":      "ethereum",
    "arbitrum":      "arbitrum-one",
    "optimism":      "optimistic-ethereum",
    "base":          "base",
    "polygon":       "polygon-pos",
    "polygon-zkevm": "polygon-zkevm",
    "zksync":        "zksync",
    "linea":         "linea",
    "scroll":        "scroll",
    "mantle":        "mantle",
    "blast":         "blast",
    # Non-EVM
    "solana":        "solana",
    "tron":          "tron",
}

# Native ticker symbol → CoinGecko coin id. Used when an asset has no
# contract (the native gas / chain token, or a CEX line like "BTC").
NATIVE_COIN_IDS: Final[dict[str, str]] = {
    "ETH":   "ethereum",
    "BTC":   "bitcoin",
    "SOL":   "solana",
    "TRX":   "tron",
    "MATIC": "matic-network",
    "BNB":   "binancecoin",
    "AVAX":  "avalanche-2",
    "MNT":   "mantle",
    # Common ERC-20 majors that travel cross-chain as the same ticker.
    # Listed here so a CEX balance like "USDC" gets priced without a
    # contract lookup.
    "USDC":  "usd-coin",
    "USDT":  "tether",
    "DAI":   "dai",
    "WBTC":  "wrapped-bitcoin",
    "WETH":  "weth",
    "LINK":  "chainlink",
    "UNI":   "uniswap",
    "ARB":   "arbitrum",
    "OP":    "optimism",
    "ADA":   "cardano",
    "DOT":   "polkadot",
    "ATOM":  "cosmos",
    "SUI":   "sui",
    "APT":   "aptos-token",
    "TON":   "the-open-network",
    "DOGE":  "dogecoin",
    "XRP":   "ripple",
    # Binance liquid-staked SOL — common on Binance accounts.
    "BNSOL": "binance-staked-sol",
}

_BASE_URL = "https://api.coingecko.com"


def _decimal_or_none(value) -> Decimal | None:
    """CoinGecko returns 0 when it has no price info — surface as None
    so the rest of the pipeline knows we couldn't price it (vs. having
    a real zero, which doesn't happen for any tradeable asset)."""
    if value is None:
        return None
    d = Decimal(str(value))
    if d <= 0:
        return None
    return d


async def fetch_native_price(
    symbol: str | None,
    *,
    http: httpx.AsyncClient | None = None,
) -> Decimal | None:
    """Return USD price for a native ticker symbol, or None if unknown.

    Hardcodes USDT == 1 since CoinGecko doesn't offer USDT as a
    `vs_currency` and we'd otherwise round-trip for a known stablecoin.
    """
    if not symbol:
        return None
    sym = symbol.strip().upper()
    if sym == "USDT":
        return Decimal("1")
    coin_id = NATIVE_COIN_IDS.get(sym)
    if not coin_id:
        return None
    owns = http is None
    client = http or httpx.AsyncClient(base_url=_BASE_URL, timeout=15.0)
    try:
        resp = await client.get(
            "/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
        )
        resp.raise_for_status()
        return _decimal_or_none(resp.json().get(coin_id, {}).get("usd"))
    except httpx.HTTPError as exc:
        log.warning("coingecko_native_price_failed", symbol=sym, error=str(exc))
        return None
    finally:
        if owns:
            await client.aclose()


async def fetch_token_prices(
    chain: str,
    contracts: list[str],
    *,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Decimal]:
    """Return ``{contract → USD price}`` for the given on-chain tokens.

    NOTE: CoinGecko's free tier (no API key) caps
    ``simple/token_price/{platform}?contract_addresses=...`` at **1
    contract per request** (error code 10012 otherwise). We loop one
    contract at a time. With ~30 calls/min free-tier limit this caps
    a single wallet at ~30 ERC-20 tokens per sync round; spam-filtered
    addresses should rarely come near that.

    Returns ``{}`` (without HTTP) when the chain isn't mapped or no
    contracts were passed. EVM contract keys are normalised so the
    caller doesn't have to worry about CoinGecko's lower-casing.
    """
    if not contracts:
        return {}
    platform = CHAIN_TO_PLATFORM.get(chain.lower())
    if not platform:
        return {}

    owns = http is None
    client = http or httpx.AsyncClient(base_url=_BASE_URL, timeout=15.0)
    out: dict[str, Decimal] = {}
    try:
        for contract in contracts:
            try:
                resp = await client.get(
                    f"/api/v3/simple/token_price/{platform}",
                    params={
                        "contract_addresses": contract,
                        "vs_currencies": "usd",
                    },
                )
                resp.raise_for_status()
                raw = resp.json()
            except httpx.HTTPError as exc:
                # Per-contract failure (404 / rate-limit / etc) skips that
                # one but doesn't abandon the rest.
                log.warning(
                    "coingecko_token_price_failed",
                    chain=chain, contract=contract, error=str(exc),
                )
                continue
            # CoinGecko lower-cases EVM contract keys; allow both casings
            # but DON'T fall through to "first key wins" — that would
            # apply the wrong price to a contract CoinGecko doesn't
            # actually know about.
            entry = raw.get(contract) or raw.get(contract.lower())
            if not entry:
                continue
            price = _decimal_or_none(entry.get("usd"))
            if price is not None:
                out[contract] = price
    finally:
        if owns:
            await client.aclose()

    return out
