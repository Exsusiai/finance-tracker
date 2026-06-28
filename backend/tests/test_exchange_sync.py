"""P1-4 A3: CEX (Binance / Bitget) spot balance providers.

Sign helpers are tested as pure functions against known vectors so a
provider regression can't quietly break authentication. End-to-end
fetches go through ``httpx.MockTransport`` — the mock handler asserts
the right auth surface (headers / signature param) is present before
returning the fixture body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.services.crypto_sync import BalanceItem  # noqa: E402
from app.services.exchange_sync import (  # noqa: E402
    ExchangeAuthError,
    dispatch,
)
from app.services.exchange_sync.binance import BinanceProvider  # noqa: E402
from app.services.exchange_sync.bitget import BitgetProvider  # noqa: E402
from app.services.exchange_sync.sign import (  # noqa: E402
    binance_signature,
    bitget_signature,
)

FIXTURES = Path(__file__).parent / "fixtures" / "exchange_sync"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _expected_items(fx: dict[str, Any]) -> list[BalanceItem]:
    return [
        BalanceItem(
            symbol=row["symbol"],
            contract=row["contract"],
            quantity=Decimal(row["quantity"]),
            decimals=row["decimals"],
        )
        for row in fx["_expected"]["balances"]
    ]


# ─── Sign helpers ──────────────────────────────────────────────────────────


class TestBinanceSign:
    def test_known_vector(self):
        """Reproducible HMAC-SHA256 hex digest."""
        qs = "timestamp=1700000000000&recvWindow=5000"
        secret = "supersecret"
        expected = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        assert binance_signature(qs, secret) == expected

    def test_empty_query_string(self):
        sig = binance_signature("", "any")
        assert sig == hmac.new(b"any", b"", hashlib.sha256).hexdigest()


class TestBitgetSign:
    def test_known_vector(self):
        ts = "1700000000000"
        method = "GET"
        path = "/api/v2/spot/account/assets"
        body = ""
        secret = "supersecret"
        payload = (ts + method + path + body).encode()
        expected = base64.b64encode(
            hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        ).decode()
        assert bitget_signature(ts, method, path, body, secret) == expected

    def test_method_normalised_to_uppercase(self):
        """Bitget docs spell ``GET`` upper-case — `get` would silently mis-sign."""
        s_upper = bitget_signature("1", "GET", "/x", "", "secret")
        s_lower = bitget_signature("1", "get", "/x", "", "secret")
        assert s_upper == s_lower


# ─── Binance ───────────────────────────────────────────────────────────────


class TestBinanceProvider:
    @pytest.mark.asyncio
    async def test_parses_spot_balances(self):
        fx = _load("binance_account.json")
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["url"] = str(request.url)
            return httpx.Response(200, json=fx["account"])

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.binance.com",
        )
        provider = BinanceProvider(http=client)
        items = await provider.fetch_balances(
            api_key="ak", api_secret="sk"
        )
        await client.aclose()

        # Auth surface assertions.
        assert captured["headers"].get("x-mbx-apikey") == "ak"
        qs = parse_qs(urlparse(captured["url"]).query)
        assert "signature" in qs, "request must include `signature` query param"
        assert "timestamp" in qs

        assert sorted(items, key=lambda x: x.symbol or "") == sorted(
            _expected_items(fx), key=lambda x: x.symbol or ""
        )

    @pytest.mark.asyncio
    async def test_auth_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"code": -2014, "msg": "API-key format invalid."},
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.binance.com",
        )
        provider = BinanceProvider(http=client)
        with pytest.raises(ExchangeAuthError):
            await provider.fetch_balances(api_key="bad", api_secret="bad")
        await client.aclose()


# ─── Bitget ────────────────────────────────────────────────────────────────


class TestBitgetProvider:
    @pytest.mark.asyncio
    async def test_parses_spot_plus_futures_aggregated(self):
        """Hits spot + 3 mix endpoints, sums (available + locked) per
        coin across all four. Confirms PnL stays out and zero-balance
        rows are dropped."""
        fx = _load("bitget_account.json")
        captured_headers: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(dict(request.headers))
            path = request.url.path
            product = request.url.params.get("productType")
            if "/spot/account/assets" in path:
                return httpx.Response(200, json=fx["spot_assets"])
            if "/mix/account/accounts" in path:
                if product == "USDT-FUTURES":
                    return httpx.Response(200, json=fx["usdt_futures"])
                if product == "USDC-FUTURES":
                    return httpx.Response(200, json=fx["usdc_futures"])
                if product == "COIN-FUTURES":
                    return httpx.Response(200, json=fx["coin_futures"])
            return httpx.Response(404, json={"code": "404", "msg": "unknown"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.bitget.com",
        )
        provider = BitgetProvider(http=client)
        items = await provider.fetch_balances(
            api_key="ak", api_secret="sk", passphrase="pp"
        )
        await client.aclose()

        # All four calls must auth.
        assert len(captured_headers) == 4
        for h in captured_headers:
            assert h.get("access-key") == "ak"
            assert h.get("access-passphrase") == "pp"
            assert h.get("access-timestamp")
            assert h.get("access-sign")

        assert sorted(items, key=lambda x: x.symbol or "") == sorted(
            _expected_items(fx), key=lambda x: x.symbol or ""
        )

    @pytest.mark.asyncio
    async def test_unified_account_switches_to_v3(self):
        """Account upgraded to Unified Account: classic spot returns HTTP 400
        + code 40085, so the provider must switch to the v3 endpoint, parse its
        consolidated `assets`, drop zero-balance coins, and NEVER touch the
        classic /mix endpoints."""
        fx = _load("bitget_unified.json")
        paths_called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths_called.append(request.url.path)
            path = request.url.path
            if "/spot/account/assets" in path:
                return httpx.Response(400, json=fx["spot_40085"])
            if "/api/v3/account/assets" in path:
                return httpx.Response(200, json=fx["v3_assets"])
            return httpx.Response(404, json={"code": "404", "msg": "unexpected call"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.bitget.com",
        )
        provider = BitgetProvider(http=client)
        items = await provider.fetch_balances(
            api_key="ak", api_secret="sk", passphrase="pp"
        )
        await client.aclose()

        # Switched to v3; classic mix endpoints were NOT called.
        assert any("/api/v3/account/assets" in p for p in paths_called)
        assert not any("/mix/account/accounts" in p for p in paths_called)
        # Real balances surfaced; the zero-balance coin is dropped.
        got = {it.symbol: it.quantity for it in items}
        assert set(got) == {"USDC", "USDT", "BTC", "BGB"}
        assert got["USDC"] == Decimal("746.95565305")
        assert got["BTC"] == Decimal("0.00327113")

    @pytest.mark.asyncio
    async def test_futures_endpoint_failure_does_not_kill_spot(self):
        """If one /mix endpoint is rate-limited or 5xx, the rest of the
        result still surfaces — we don't lose the spot balances."""
        fx = _load("bitget_account.json")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/spot/account/assets" in path:
                return httpx.Response(200, json=fx["spot_assets"])
            # ALL futures endpoints fail.
            return httpx.Response(429, json={"code": "429", "msg": "rate limit"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.bitget.com",
        )
        provider = BitgetProvider(http=client)
        items = await provider.fetch_balances(
            api_key="ak", api_secret="sk", passphrase="pp"
        )
        await client.aclose()

        # Spot balances still made it through (BTC 0.25, USDT 500, SOL 10).
        symbols = {it.symbol for it in items}
        assert symbols == {"BTC", "USDT", "SOL"}

    @pytest.mark.asyncio
    async def test_passphrase_required(self):
        """Bitget *always* needs a passphrase — calling without one must
        fail before we ever hit the wire."""
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
            base_url="https://api.bitget.com",
        )
        provider = BitgetProvider(http=client)
        with pytest.raises(ValueError):
            await provider.fetch_balances(api_key="ak", api_secret="sk", passphrase=None)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_business_error_raises(self):
        """Bitget returns 200 + non-zero `code` for auth failures."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": "40006", "msg": "Invalid sign", "data": []},
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.bitget.com",
        )
        provider = BitgetProvider(http=client)
        with pytest.raises(ExchangeAuthError):
            await provider.fetch_balances(
                api_key="ak", api_secret="sk", passphrase="pp"
            )
        await client.aclose()


# ─── Dispatcher ────────────────────────────────────────────────────────────


class TestDispatch:
    def test_binance(self):
        assert isinstance(dispatch("binance"), BinanceProvider)

    def test_bitget(self):
        assert isinstance(dispatch("bitget"), BitgetProvider)

    def test_case_insensitive(self):
        assert isinstance(dispatch("Binance"), BinanceProvider)
        assert isinstance(dispatch("BITGET"), BitgetProvider)

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            dispatch("kraken")
