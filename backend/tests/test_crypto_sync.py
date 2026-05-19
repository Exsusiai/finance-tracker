"""P1-4 A2: chain balance providers.

Tests the Provider Protocol + 4 chain-specific implementations using
``httpx.MockTransport`` so nothing hits real networks. Fixtures live in
``tests/fixtures/crypto_sync/*.json`` and embed both the upstream response
shapes and the expected parsed output (under ``_expected``), so a fixture
update is a one-stop edit.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.services.crypto_sync import (  # noqa: E402
    BalanceItem,
    CryptoChainProvider,
    dispatch,
)
from app.services.crypto_sync.btc_blockstream import BlockstreamProvider  # noqa: E402
from app.services.crypto_sync.evm_alchemy import AlchemyEVMProvider  # noqa: E402
from app.services.crypto_sync.sol_rpc import SolanaRPCProvider  # noqa: E402
from app.services.crypto_sync.tron_grid import TronGridProvider  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "crypto_sync"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _expected_items(fixture: dict[str, Any]) -> list[BalanceItem]:
    return [
        BalanceItem(
            symbol=row["symbol"],
            contract=row["contract"],
            quantity=Decimal(row["quantity"]),
            decimals=row["decimals"],
        )
        for row in fixture["_expected"]["balances"]
    ]


# ─── Provider Protocol ─────────────────────────────────────────────────────


class TestProviderProtocol:
    def test_balance_item_shape(self):
        item = BalanceItem(symbol="ETH", contract=None, quantity=Decimal("1.5"), decimals=18)
        assert item.symbol == "ETH"
        assert item.contract is None
        assert item.quantity == Decimal("1.5")
        assert item.decimals == 18

    def test_providers_satisfy_protocol(self):
        """Each provider is structurally compatible with CryptoChainProvider."""
        # The Protocol check is structural at runtime in py>=3.12; we just
        # verify the public surface exists.
        for cls in (AlchemyEVMProvider, BlockstreamProvider, SolanaRPCProvider, TronGridProvider):
            assert hasattr(cls, "fetch_balances")
            assert hasattr(cls, "chain_id")


# ─── Alchemy EVM ───────────────────────────────────────────────────────────


class TestAlchemyEVMProvider:
    @pytest.mark.asyncio
    async def test_parses_native_and_erc20(self):
        fx = _load("alchemy_evm.json")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            method = body.get("method")
            if method == "eth_getBalance":
                return httpx.Response(200, json=fx["eth_getBalance"])
            if method == "alchemy_getTokenBalances":
                return httpx.Response(200, json=fx["alchemy_getTokenBalances"])
            if method == "alchemy_getTokenMetadata":
                contract = body["params"][0].lower()
                return httpx.Response(200, json=fx["alchemy_getTokenMetadata"][contract])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, base_url="https://eth-mainnet.g.alchemy.com")

        provider = AlchemyEVMProvider(chain="ethereum", api_key="test-key", http=client)
        items = await provider.fetch_balances("0x1111111111111111111111111111111111111111")
        await client.aclose()

        # Order-independent comparison.
        assert sorted(items, key=lambda x: (x.symbol or "", x.contract or "")) == sorted(
            _expected_items(fx), key=lambda x: (x.symbol or "", x.contract or "")
        )

    def test_known_chain_endpoint_mapping(self):
        for chain in ("ethereum", "arbitrum", "optimism", "base", "polygon"):
            p = AlchemyEVMProvider(chain=chain, api_key="k")
            assert p.endpoint.startswith("https://")
            assert "alchemy.com" in p.endpoint

    def test_unknown_chain_rejected(self):
        with pytest.raises(ValueError):
            AlchemyEVMProvider(chain="not_a_chain", api_key="k")


# ─── Blockstream BTC ───────────────────────────────────────────────────────


class TestBlockstreamProvider:
    @pytest.mark.asyncio
    async def test_parses_confirmed_balance(self):
        fx = _load("blockstream_btc.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=fx["address_summary"])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://blockstream.info")
        provider = BlockstreamProvider(http=client)
        items = await provider.fetch_balances("bc1qexampleaddressxxxxxxxxxxxxxxxxxxxxx")
        await client.aclose()

        assert items == _expected_items(fx)

    @pytest.mark.asyncio
    async def test_zero_balance_returns_empty(self):
        """A wallet with funded == spent should yield no rows (don't store zero rows)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "chain_stats": {"funded_txo_sum": 100, "spent_txo_sum": 100},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://blockstream.info")
        provider = BlockstreamProvider(http=client)
        items = await provider.fetch_balances("bc1qzero")
        await client.aclose()
        assert items == []


# ─── Solana RPC ────────────────────────────────────────────────────────────


class TestSolanaRPCProvider:
    @pytest.mark.asyncio
    async def test_parses_native_and_spl(self):
        fx = _load("solana_rpc.json")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            method = body.get("method")
            if method == "getBalance":
                return httpx.Response(200, json=fx["getBalance"])
            if method == "getTokenAccountsByOwner":
                return httpx.Response(200, json=fx["getTokenAccountsByOwner"])
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.mainnet-beta.solana.com")
        provider = SolanaRPCProvider(http=client)
        items = await provider.fetch_balances("ExampleOwnerXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
        await client.aclose()

        # Items don't have stable symbol for SPL tokens (mint = contract).
        assert sorted(items, key=lambda x: (x.contract or "")) == sorted(
            _expected_items(fx), key=lambda x: (x.contract or "")
        )


# ─── TronGrid ──────────────────────────────────────────────────────────────


class TestTronGridProvider:
    @pytest.mark.asyncio
    async def test_parses_trx_and_trc20(self):
        fx = _load("trongrid_tron.json")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.startswith("/v1/accounts/") and "/" not in path[len("/v1/accounts/") :]:
                return httpx.Response(200, json=fx["account"])
            if path.startswith("/v1/contracts/"):
                contract = path.rsplit("/", 1)[-1]
                meta = fx["contract_metadata"].get(contract)
                if meta is None:
                    return httpx.Response(404)
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "symbol": meta["symbol"],
                                "decimals": meta["decimals"],
                                "name": meta["name"],
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.trongrid.io")
        provider = TronGridProvider(http=client)
        items = await provider.fetch_balances("ExampleTronAddressXXXXXXXXXXXXXXXX")
        await client.aclose()

        assert sorted(items, key=lambda x: (x.symbol or "", x.contract or "")) == sorted(
            _expected_items(fx), key=lambda x: (x.symbol or "", x.contract or "")
        )


# ─── Dispatcher ────────────────────────────────────────────────────────────


class TestDispatch:
    def test_evm_chains_route_to_alchemy(self):
        for chain in ("ethereum", "arbitrum", "optimism", "base", "polygon"):
            p = dispatch(chain=chain, alchemy_api_key="k")
            assert isinstance(p, AlchemyEVMProvider)

    def test_bitcoin_routes_to_blockstream(self):
        p = dispatch(chain="bitcoin", alchemy_api_key=None)
        assert isinstance(p, BlockstreamProvider)

    def test_solana_routes_to_sol_rpc(self):
        p = dispatch(chain="solana", alchemy_api_key=None)
        assert isinstance(p, SolanaRPCProvider)

    def test_tron_routes_to_tron_grid(self):
        p = dispatch(chain="tron", alchemy_api_key=None)
        assert isinstance(p, TronGridProvider)

    def test_unknown_chain_raises(self):
        with pytest.raises(ValueError):
            dispatch(chain="cardano", alchemy_api_key=None)

    def test_evm_without_api_key_raises(self):
        with pytest.raises(ValueError):
            dispatch(chain="ethereum", alchemy_api_key=None)
