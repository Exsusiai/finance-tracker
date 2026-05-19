"""P1.2: CoinGecko price discovery for wallet/exchange-synced assets.

Three call shapes covered:
- Native chain coin price: GET /api/v3/simple/price?ids=<coin>&vs_currencies=usd
- Token by contract:      GET /api/v3/simple/token_price/<platform>?contract_addresses=…
- Multiple contracts in a single batched call (we keep per-platform
  request count low to respect the free tier's 10-30 req/min cap).

All network IO is mocked via httpx.MockTransport. USDT is hard-coded to
1.0 to avoid a needless round-trip for the stablecoin we already use as
the valuation unit.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.services.market_data.coingecko import (
    CHAIN_TO_PLATFORM,
    NATIVE_COIN_IDS,
    fetch_native_price,
    fetch_token_prices,
)


class TestStaticMaps:
    def test_native_coin_ids_cover_supported_chains(self):
        for sym in ("ETH", "BTC", "SOL", "TRX", "MATIC"):
            assert sym in NATIVE_COIN_IDS, f"{sym} missing from NATIVE_COIN_IDS"

    def test_chain_to_platform_evm(self):
        # EVM chains we hit Alchemy for must resolve to a CoinGecko platform.
        assert CHAIN_TO_PLATFORM["ethereum"] == "ethereum"
        assert CHAIN_TO_PLATFORM["arbitrum"] == "arbitrum-one"
        assert CHAIN_TO_PLATFORM["optimism"] == "optimistic-ethereum"
        assert CHAIN_TO_PLATFORM["base"] == "base"
        assert CHAIN_TO_PLATFORM["polygon"] == "polygon-pos"

    def test_chain_to_platform_non_evm(self):
        assert CHAIN_TO_PLATFORM["solana"] == "solana"
        assert CHAIN_TO_PLATFORM["tron"] == "tron"


class TestFetchNativePrice:
    @pytest.mark.asyncio
    async def test_eth_price_in_usdt(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/v3/simple/price" in request.url.path
            assert request.url.params.get("ids") == "ethereum"
            assert request.url.params.get("vs_currencies") == "usd"
            return httpx.Response(200, json={"ethereum": {"usd": 3200.5}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coingecko.com",
        )
        price = await fetch_native_price("ETH", http=client)
        await client.aclose()
        assert price == Decimal("3200.5")

    @pytest.mark.asyncio
    async def test_usdt_is_one(self):
        # No HTTP call: USDT is the unit, hard-coded to 1.
        price = await fetch_native_price("USDT")
        assert price == Decimal("1")

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_none(self):
        # No NATIVE_COIN_IDS entry → don't even try.
        price = await fetch_native_price("?MADEUP")
        assert price is None

    @pytest.mark.asyncio
    async def test_upstream_zero_returns_none(self):
        """CoinGecko returns 0 when it doesn't actually know a price — we
        treat that as missing rather than writing a misleading 0 price."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ethereum": {"usd": 0}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coingecko.com",
        )
        price = await fetch_native_price("ETH", http=client)
        await client.aclose()
        assert price is None


class TestFetchTokenPrices:
    @pytest.mark.asyncio
    async def test_per_contract_loop(self):
        """CoinGecko free tier caps to 1 contract per call (error 10012
        otherwise). The fetcher must therefore loop one contract at a
        time, not batch."""
        usdc = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
        usdc_e = "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/v3/simple/token_price/arbitrum-one" in request.url.path
            contract = request.url.params.get("contract_addresses") or ""
            calls.append(contract)
            # Every request hits with exactly ONE contract.
            assert "," not in contract, "batched calls are no longer allowed"
            mapping = {usdc: 1.0001, usdc_e: 0.9998}
            return httpx.Response(
                200,
                json={contract: {"usd": mapping[contract]}},
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coingecko.com",
        )
        prices = await fetch_token_prices(
            chain="arbitrum",
            contracts=[usdc, usdc_e],
            http=client,
        )
        await client.aclose()

        assert calls == [usdc, usdc_e]  # one call per contract, in order
        assert prices[usdc] == Decimal("1.0001")
        assert prices[usdc_e] == Decimal("0.9998")

    @pytest.mark.asyncio
    async def test_per_contract_failure_does_not_abort_others(self):
        """A 4xx on one contract should not kill the rest of the loop."""
        good = "0xgoodbeef"
        bad = "0xbadbeef"

        def handler(request: httpx.Request) -> httpx.Response:
            contract = request.url.params.get("contract_addresses") or ""
            if contract == bad:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={contract: {"usd": 1.23}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coingecko.com",
        )
        prices = await fetch_token_prices(
            chain="ethereum",
            contracts=[bad, good],
            http=client,
        )
        await client.aclose()

        assert good in prices and prices[good] == Decimal("1.23")
        assert bad not in prices

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_empty(self):
        prices = await fetch_token_prices(
            chain="cardano",  # not in CHAIN_TO_PLATFORM yet
            contracts=["addr1..."],
        )
        assert prices == {}

    @pytest.mark.asyncio
    async def test_empty_contracts_returns_empty_without_http(self):
        # No contracts → must not hit the network.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not have called CoinGecko")

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coingecko.com",
        )
        prices = await fetch_token_prices(chain="ethereum", contracts=[], http=client)
        await client.aclose()
        assert prices == {}

    @pytest.mark.asyncio
    async def test_missing_contract_dropped(self):
        """A contract CoinGecko doesn't know about: returns no entry → we
        omit it from the result rather than mapping it to 0."""
        usdc = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
        ghost = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={usdc: {"usd": 1.0}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coingecko.com",
        )
        prices = await fetch_token_prices(
            chain="arbitrum", contracts=[usdc, ghost], http=client
        )
        await client.aclose()

        assert usdc in prices
        assert ghost not in prices
