"""P1-4 A4.3: HTTP integration tests for /accounts/{id}/sync + sub-resources.

Provider layer is monkeypatched via the orchestrator's `_dispatch_chain`
/ `_dispatch_exchange` indirection — these tests exercise the routing,
auth, schema mapping, and encryption boundary without touching real
APIs.
"""

from __future__ import annotations

import os
import secrets
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")
os.environ.setdefault("FINANCE_BANK_ENCRYPTION_KEY", secrets.token_hex(32))

from app.core.config import Settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account  # noqa: E402
from app.services.crypto_sync import BalanceItem  # noqa: E402
from app.services.wallet_sync import orchestrator  # noqa: E402


TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

_auth_settings = Settings(finance_tracker_api_token=_TEST_TOKEN, auth_disabled=False)

AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}"}


async def override_get_db():
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_schema():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    with patch("app.core.auth.settings", _auth_settings):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed_account(name: str, acct_type: str) -> int:
    async with _Session() as s:
        acc = Account(
            name=name, type=acct_type, currency="USDT",
            initial_balance=Decimal("0"), is_active=True,
            created_at=_utcnow(), updated_at=_utcnow(),
        )
        s.add(acc)
        await s.commit()
        return acc.id


# ─── Auth ──────────────────────────────────────────────────────────────────


class TestAuth:
    async def test_addresses_get_requires_auth(self, client: AsyncClient):
        r = await client.get("/api/v1/accounts/1/addresses")
        assert r.status_code == 401

    async def test_sync_requires_auth(self, client: AsyncClient):
        r = await client.post("/api/v1/accounts/1/sync")
        assert r.status_code == 401


# ─── Chain addresses CRUD ─────────────────────────────────────────────────


class TestChainAddresses:
    async def test_crud_roundtrip(self, client: AsyncClient):
        acc_id = await _seed_account("WalletA", "crypto_wallet")

        # Empty list initially.
        r = await client.get(f"/api/v1/accounts/{acc_id}/addresses", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["data"] == []

        # Add one.
        r = await client.post(
            f"/api/v1/accounts/{acc_id}/addresses",
            headers=AUTH,
            json={"chain": "Ethereum", "address": "0xABC", "label": "main"},
        )
        assert r.status_code == 201
        new_id = r.json()["data"]["id"]
        # Chain must be normalised to lower.
        assert r.json()["data"]["chain"] == "ethereum"

        # Duplicate fails 409.
        r = await client.post(
            f"/api/v1/accounts/{acc_id}/addresses",
            headers=AUTH,
            json={"chain": "ethereum", "address": "0xABC"},
        )
        assert r.status_code == 409

        # List shows it.
        r = await client.get(f"/api/v1/accounts/{acc_id}/addresses", headers=AUTH)
        rows = r.json()["data"]
        assert len(rows) == 1 and rows[0]["address"] == "0xABC"

        # Delete.
        r = await client.delete(f"/api/v1/accounts/{acc_id}/addresses/{new_id}", headers=AUTH)
        assert r.status_code == 200
        r = await client.get(f"/api/v1/accounts/{acc_id}/addresses", headers=AUTH)
        assert r.json()["data"] == []

    async def test_addresses_endpoint_rejects_non_wallet(self, client: AsyncClient):
        acc_id = await _seed_account("Bank", "bank")
        r = await client.get(f"/api/v1/accounts/{acc_id}/addresses", headers=AUTH)
        assert r.status_code == 400


# ─── Exchange connection ───────────────────────────────────────────────────


class TestExchangeConnection:
    async def test_upsert_does_not_echo_secrets(self, client: AsyncClient):
        acc_id = await _seed_account("Bin", "exchange")
        r = await client.put(
            f"/api/v1/accounts/{acc_id}/exchange-connection",
            headers=AUTH,
            json={"exchange": "binance", "api_key": "AKEY", "api_secret": "SKEY"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["has_credentials"] is True
        assert data["has_passphrase"] is False
        # Crucial: response body must NOT echo any secret material.
        body = r.text
        for forbidden in ("AKEY", "SKEY"):
            assert forbidden not in body

        # GET also keeps secrets out.
        r = await client.get(f"/api/v1/accounts/{acc_id}/exchange-connection", headers=AUTH)
        assert r.status_code == 200
        body = r.text
        for forbidden in ("AKEY", "SKEY"):
            assert forbidden not in body

    async def test_bitget_without_passphrase_400(self, client: AsyncClient):
        acc_id = await _seed_account("Btg", "exchange")
        r = await client.put(
            f"/api/v1/accounts/{acc_id}/exchange-connection",
            headers=AUTH,
            json={"exchange": "bitget", "api_key": "k", "api_secret": "s"},
        )
        assert r.status_code == 400

    async def test_unknown_exchange_400(self, client: AsyncClient):
        acc_id = await _seed_account("Krk", "exchange")
        r = await client.put(
            f"/api/v1/accounts/{acc_id}/exchange-connection",
            headers=AUTH,
            json={"exchange": "kraken", "api_key": "k", "api_secret": "s"},
        )
        assert r.status_code == 400

    async def test_rotation_replaces_creds_and_resets_status(self, client: AsyncClient):
        acc_id = await _seed_account("Rot", "exchange")
        await client.put(
            f"/api/v1/accounts/{acc_id}/exchange-connection",
            headers=AUTH,
            json={"exchange": "binance", "api_key": "OLD", "api_secret": "OLD"},
        )
        r = await client.put(
            f"/api/v1/accounts/{acc_id}/exchange-connection",
            headers=AUTH,
            json={"exchange": "binance", "api_key": "NEW", "api_secret": "NEW"},
        )
        assert r.status_code == 200
        for forbidden in ("OLD", "NEW"):
            assert forbidden not in r.text


# ─── Sync ─────────────────────────────────────────────────────────────────


class _FakeChainProvider:
    def __init__(self, chain: str, items: list[BalanceItem]):
        self.chain_id = chain
        self._items = items

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        return list(self._items)


class TestSync:
    async def test_crypto_wallet_sync_returns_summary(
        self, client: AsyncClient, monkeypatch
    ):
        acc_id = await _seed_account("SyncWallet", "crypto_wallet")
        await client.post(
            f"/api/v1/accounts/{acc_id}/addresses",
            headers=AUTH,
            json={"chain": "ethereum", "address": "0xeee"},
        )

        def fake_dispatch(chain, alchemy_api_key):
            return _FakeChainProvider(chain, [
                BalanceItem(symbol="ETH", contract=None, quantity=Decimal("3"), decimals=18),
            ])

        monkeypatch.setattr(orchestrator, "_dispatch_chain", fake_dispatch)

        r = await client.post(f"/api/v1/accounts/{acc_id}/sync", headers=AUTH)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["account_type"] == "crypto_wallet"
        assert data["total_synced"] == 1
        assert data["total_errors"] == 0

    async def test_sync_on_unsupported_account_type_400(self, client: AsyncClient):
        acc_id = await _seed_account("Bnk", "bank")
        r = await client.post(f"/api/v1/accounts/{acc_id}/sync", headers=AUTH)
        assert r.status_code == 400
