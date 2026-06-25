"""Regression tests for Code Review V7 fund-accounting / invariant fixes.

Covers the entrypoints the review flagged as under-tested (§P2-8):

- §P1-1  brokerage /accounts/balances reports value in the ACCOUNT's currency,
         not base currency mislabelled as the account currency.
- §P1-2  snapshot accounts (brokerage/crypto/exchange) reject balance
         adjustment server-side (no phantom cash ledger).
- §P1-3  a paired transfer is counted ONCE in /cashflow/monthly (not 2×).
- §P1-5  AccountUpdate keeps the crypto/exchange ⇒ USDT invariant.

All tests drive the real ASGI app over httpx against an in-memory SQLite.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_TOKEN = "v7v7v7v7v7v7v7v7v7v7v7v7v7v7v7v7v7v7v7v7"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Asset,
    AssetHolding,
    Category,
    MarketPrice,
    Transaction,
)

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


# ─── §P1-3 monthly transfer dedup ────────────────────────────────────────────


async def test_monthly_counts_transfer_pair_once(client: AsyncClient, db: AsyncSession):
    """/cashflow/monthly transfer total must count a pair ONCE (was 2×)."""
    from app.core.config import get_settings
    cur = get_settings().base_currency

    a = Account(name="V7-mon-A", type="bank", currency=cur, initial_balance=Decimal("0"),
                is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    b = Account(name="V7-mon-B", type="bank", currency=cur, initial_balance=Decimal("0"),
                is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    cat = Category(name="跨行划转", kind="transfer", parent_id=None, is_system=False)
    db.add_all([a, b, cat])
    await db.flush()

    leg1 = Transaction(account_id=a.id, category_id=cat.id, occurred_at="2026-08-10T00:00:00Z",
                       amount=Decimal("700"), currency=cur, type="transfer", source="manual",
                       is_pending=False, created_at=_utcnow(), updated_at=_utcnow())
    leg2 = Transaction(account_id=b.id, category_id=cat.id, occurred_at="2026-08-10T00:00:00Z",
                       amount=Decimal("700"), currency=cur, type="transfer", source="manual",
                       is_pending=False, created_at=_utcnow(), updated_at=_utcnow())
    db.add_all([leg1, leg2])
    await db.flush()
    leg1.metadata_json = json.dumps({"transfer_direction": "out", "paired_with_tx_id": leg2.id})
    leg2.metadata_json = json.dumps({"transfer_direction": "in", "paired_with_tx_id": leg1.id})
    await db.commit()

    resp = await client.get("/api/v1/cashflow/monthly",
                            params={"from": "2026-08", "to": "2026-08"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["data"] if r["period"] == "2026-08")
    assert Decimal(row["transfer"]) == Decimal("700"), f"transfer double-counted: {row}"


# ─── §P1-1 brokerage balance currency ────────────────────────────────────────


async def test_brokerage_balance_in_account_currency(client: AsyncClient, db: AsyncSession):
    """A brokerage account priced in its own (non-base) currency must report
    holdings value in THAT currency. The old code converted to base currency
    but labelled it as the account currency (and, with no FX path to base,
    dropped the holding to 0)."""
    acc = Account(name="V7-SGD-broker", type="brokerage", currency="SGD",
                  initial_balance=Decimal("0"), is_active=True,
                  created_at=_utcnow(), updated_at=_utcnow())
    asset = Asset(symbol="D05", name="DBS", asset_class="other", currency="SGD",
                  data_source="ibkr", data_source_id="C1", chain="", contract="")
    db.add_all([acc, asset])
    await db.flush()
    db.add(AssetHolding(account_id=acc.id, asset_id=asset.id, chain="",
                        quantity=Decimal("10"), is_active=True, source="ibkr",
                        created_at=_utcnow(), updated_at=_utcnow()))
    db.add(MarketPrice(asset_id=asset.id, quoted_at=_utcnow(), price=Decimal("20"),
                       currency="SGD", source="ibkr"))
    await db.commit()

    resp = await client.get("/api/v1/accounts/balances", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["data"] if r["account_name"] == "V7-SGD-broker")
    assert row["currency"] == "SGD"
    # 10 × 20 SGD = 200 SGD, no FX conversion (account currency == price currency).
    assert Decimal(row["balance"]) == Decimal("200"), f"wrong unit/value: {row}"


# ─── §P1-2 snapshot accounts reject balance adjustment ───────────────────────


@pytest.mark.parametrize("acct_type", ["brokerage", "crypto_wallet", "exchange"])
async def test_snapshot_account_rejects_adjust_balance(
    client: AsyncClient, db: AsyncSession, acct_type: str
):
    """Adjusting a snapshot account's balance would inject a phantom adjustment
    tx that double-counts against net worth — the backend must reject it."""
    currency = "USDT" if acct_type in ("crypto_wallet", "exchange") else "EUR"
    acc = Account(name=f"V7-snap-{acct_type}", type=acct_type, currency=currency,
                  initial_balance=Decimal("0"), is_active=True,
                  created_at=_utcnow(), updated_at=_utcnow())
    db.add(acc)
    await db.commit()

    resp = await client.post(f"/api/v1/accounts/{acc.id}/adjust-balance",
                             json={"target_balance": "1000"}, headers=AUTH_HEADERS)
    assert resp.status_code == 400, resp.text


# ─── §P1-5 AccountUpdate USDT invariant ──────────────────────────────────────


async def test_patch_cannot_break_crypto_usdt_invariant(client: AsyncClient, db: AsyncSession):
    """PATCH must not let a crypto/exchange account leave USDT, nor flip a fiat
    account into crypto with a non-USDT currency."""
    crypto = Account(name="V7-crypto", type="crypto_wallet", currency="USDT",
                     initial_balance=Decimal("0"), is_active=True,
                     created_at=_utcnow(), updated_at=_utcnow())
    fiat = Account(name="V7-fiat", type="bank", currency="EUR",
                   initial_balance=Decimal("0"), is_active=True,
                   created_at=_utcnow(), updated_at=_utcnow())
    db.add_all([crypto, fiat])
    await db.commit()

    # Changing an existing crypto account's currency away from USDT → rejected.
    r1 = await client.patch(f"/api/v1/accounts/{crypto.id}",
                            json={"currency": "EUR"}, headers=AUTH_HEADERS)
    assert r1.status_code == 400, r1.text

    # Flipping a fiat account to crypto while keeping EUR → rejected.
    r2 = await client.patch(f"/api/v1/accounts/{fiat.id}",
                            json={"type": "crypto_wallet"}, headers=AUTH_HEADERS)
    assert r2.status_code == 400, r2.text

    # A valid combo (set currency to USDT then type) still works.
    r3 = await client.patch(f"/api/v1/accounts/{fiat.id}",
                            json={"type": "exchange", "currency": "USDT"}, headers=AUTH_HEADERS)
    assert r3.status_code == 200, r3.text


pytestmark = pytest.mark.asyncio
