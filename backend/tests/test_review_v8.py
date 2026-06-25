"""Regression tests for Code Review V8 — residual gaps in the V7 fixes.

- §P1-3  snapshot accounts truly have no cash ledger: create rejects non-zero
         initial_balance, transaction create is refused, and /accounts/balances
         ignores any ledger that slipped in directly.

(§P1-1 broker reclaim lives in test_broker_sync.py; §P2-1 rollback cancel in
 test_llm_dispatch_race.py; §P1-4 MCP snapshot dedup shares cashflow logic.)
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_TOKEN = "v8v8v8v8v8v8v8v8v8v8v8v8v8v8v8v8v8v8v8v8"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Asset, AssetHolding, MarketPrice, Transaction  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}", "Content-Type": "application/json"}


async def _override_get_db():
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _create_tables():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy import text as _text

        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        await conn.execute(_text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(_text(_BALANCE_VIEW_SQL))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture()
async def db():
    async with _Session() as session:
        yield session


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_create_brokerage_rejects_nonzero_initial_balance(client: AsyncClient):
    """POST /accounts for a snapshot account with initial_balance != 0 → 422."""
    resp = await client.post("/api/v1/accounts", headers=AUTH_HEADERS, json={
        "name": "V8-broker-bad", "type": "brokerage", "currency": "EUR",
        "initial_balance": "1000",
    })
    assert resp.status_code == 422, resp.text

    # Zero is fine.
    ok = await client.post("/api/v1/accounts", headers=AUTH_HEADERS, json={
        "name": "V8-broker-ok", "type": "brokerage", "currency": "EUR",
        "initial_balance": "0",
    })
    assert ok.status_code == 201, ok.text


async def test_create_transaction_on_snapshot_account_rejected(client: AsyncClient, db: AsyncSession):
    """POST /transactions targeting a snapshot account → rejected (422)."""
    acc = Account(name="V8-crypto-notx", type="crypto_wallet", currency="USDT",
                  initial_balance=Decimal("0"), is_active=True,
                  created_at=_utcnow(), updated_at=_utcnow())
    db.add(acc)
    await db.commit()

    resp = await client.post("/api/v1/transactions", headers=AUTH_HEADERS, json={
        "account_id": acc.id, "occurred_at": "2026-09-01T00:00:00Z",
        "amount": "50", "currency": "USDT", "type": "expense", "source": "manual",
    })
    assert resp.status_code == 422, resp.text


async def test_balances_ignores_snapshot_ledger(client: AsyncClient, db: AsyncSession):
    """Even if a ledger row slips into a snapshot account directly (bypassing the
    API guard), /accounts/balances must report only the holdings value."""
    acc = Account(name="V8-broker-ledger", type="brokerage", currency="EUR",
                  initial_balance=Decimal("0"), is_active=True,
                  created_at=_utcnow(), updated_at=_utcnow())
    asset = Asset(symbol="V8AST", name="V8 Asset", asset_class="eu_stock", currency="EUR",
                  data_source="ibkr", data_source_id="V8C", chain="", contract="")
    db.add_all([acc, asset])
    await db.flush()
    # Holdings worth 300 EUR.
    db.add(AssetHolding(account_id=acc.id, asset_id=asset.id, chain="",
                        quantity=Decimal("3"), is_active=True, source="ibkr",
                        created_at=_utcnow(), updated_at=_utcnow()))
    db.add(MarketPrice(asset_id=asset.id, quoted_at=_utcnow(), price=Decimal("100"),
                       currency="EUR", source="ibkr"))
    # A phantom cash ledger row injected directly (e.g. legacy data).
    db.add(Transaction(account_id=acc.id, occurred_at="2026-09-02T00:00:00Z",
                       amount=Decimal("5000"), currency="EUR", type="adjustment",
                       source="manual", is_pending=False,
                       created_at=_utcnow(), updated_at=_utcnow()))
    await db.commit()

    resp = await client.get("/api/v1/accounts/balances", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["data"] if r["account_name"] == "V8-broker-ledger")
    assert Decimal(row["balance"]) == Decimal("300"), f"ledger leaked into snapshot balance: {row}"


pytestmark = pytest.mark.asyncio
