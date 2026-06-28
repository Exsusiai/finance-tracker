"""Regression test for V4-P0-1: /cashflow/by-category must NOT raw-fold
foreign-currency rows that lack FX. Behaviour should match /cashflow/monthly.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, Transaction  # noqa: E402

from httpx import AsyncClient, ASGITransport  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

AUTH_HEADERS = {
    "Authorization": f"Bearer {_TEST_TOKEN}",
    "Content-Type": "application/json",
}


async def _override_get_db():
    async with _TestingSessionLocal() as session:
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
        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        from sqlalchemy import text as _text
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
    async with _TestingSessionLocal() as session:
        yield session


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_by_category_excludes_fx_missing_rows(
    client: AsyncClient, db: AsyncSession,
):
    """A 100 GBP expense without base_amount/fx_rate must NOT show up as
    100 EUR in by-category. Previously the COALESCE(...amount) raw fallback
    silently inflated the EUR-denominated breakdown."""
    acc = Account(
        name="GBP-Acc", type="bank", currency="GBP",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    cat = Category(name="食物", kind="expense", parent_id=None, is_system=False)
    db.add(cat)
    await db.flush()

    # Foreign-currency row WITHOUT fx_rate / base_amount
    tx = Transaction(
        account_id=acc.id,
        category_id=cat.id,
        occurred_at="2026-04-15T00:00:00Z",
        amount=Decimal("100"),
        currency="GBP",
        type="expense",
        source="manual",
        is_pending=False,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(tx)
    await db.commit()

    resp = await client.get(
        "/api/v1/cashflow/by-category",
        params={"period": "2026-04"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    # The 食物 row must NOT have total=100. Either it's absent (because
    # excluded by NULL) or its total is 0 (everything filtered out).
    food_rows = [r for r in rows if r["category_name"] == "食物"]
    if food_rows:
        # Allowed: the row appears but total is 0 (NULL summed to 0)
        assert Decimal(food_rows[0]["total"]) == Decimal("0"), (
            f"FX-missing GBP row leaked into base-currency breakdown: {food_rows[0]}"
        )


async def test_by_category_counts_transfer_pair_once(
    client: AsyncClient, db: AsyncSession,
):
    """A transfer is ONE event recorded as TWO legs (real + paired mirror),
    both carrying the same transfer-kind category. by-category must count it
    ONCE, not double it (regression: 借还钱 breakdown showed 2× every loan)."""
    import json

    from app.core.config import get_settings
    # Use the runtime base currency (other test files may have set BASE_CURRENCY
    # before our setdefault) so the FX fold yields the raw amount, not NULL.
    cur = get_settings().base_currency

    a = Account(name="Real-A", type="bank", currency=cur, initial_balance=Decimal("0"),
                is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    b = Account(name="Ledger-B", type="other", currency=cur, initial_balance=Decimal("0"),
                is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    cat = Category(name="借出", kind="transfer", parent_id=None, is_system=False)
    db.add_all([a, b, cat])
    await db.flush()

    leg1 = Transaction(
        account_id=a.id, category_id=cat.id, occurred_at="2026-07-10T00:00:00Z",
        amount=Decimal("500"), currency=cur, type="transfer", source="manual",
        is_pending=False, created_at=_utcnow(), updated_at=_utcnow(),
    )
    leg2 = Transaction(
        account_id=b.id, category_id=cat.id, occurred_at="2026-07-10T00:00:00Z",
        amount=Decimal("500"), currency=cur, type="transfer", source="manual",
        is_pending=False, created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add_all([leg1, leg2])
    await db.flush()
    # Symmetric pairing pointers (as pair_transactions / synthetic-mirror write).
    leg1.metadata_json = json.dumps({"transfer_direction": "out", "paired_with_tx_id": leg2.id})
    leg2.metadata_json = json.dumps({"transfer_direction": "in", "paired_with_tx_id": leg1.id})
    await db.commit()

    resp = await client.get(
        "/api/v1/cashflow/by-category",
        params={"period": "2026-07"}, headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json()["data"] if r["category_name"] == "借出"]
    assert len(rows) == 1
    assert Decimal(rows[0]["total"]) == Decimal("500"), f"double-counted: {rows[0]}"
    assert rows[0]["count"] == 1


async def test_by_category_range_aggregates_months(
    client: AsyncClient, db: AsyncSession,
):
    """`from`/`to` aggregates a month range into one breakdown; a single
    `period` returns only that month (req 2026-06-27: analytics category
    distribution supports multi-month aggregation)."""
    from app.core.config import get_settings
    cur = get_settings().base_currency

    acc = Account(name="Range-Acc", type="bank", currency=cur, initial_balance=Decimal("0"),
                  is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    cat = Category(name="区间餐饮", kind="expense", parent_id=None, is_system=False)
    db.add_all([acc, cat])
    await db.flush()
    db.add_all([
        Transaction(account_id=acc.id, category_id=cat.id, occurred_at="2026-09-05T00:00:00Z",
                    amount=Decimal("30"), currency=cur, type="expense", source="manual",
                    is_pending=False, created_at=_utcnow(), updated_at=_utcnow()),
        Transaction(account_id=acc.id, category_id=cat.id, occurred_at="2026-10-05T00:00:00Z",
                    amount=Decimal("70"), currency=cur, type="expense", source="manual",
                    is_pending=False, created_at=_utcnow(), updated_at=_utcnow()),
    ])
    await db.commit()

    def _total(rows):
        r = [x for x in rows if x["category_name"] == "区间餐饮"]
        return Decimal(r[0]["total"]) if r else Decimal("0")

    single = await client.get("/api/v1/cashflow/by-category",
                              params={"period": "2026-09"}, headers=AUTH_HEADERS)
    assert single.status_code == 200, single.text
    assert _total(single.json()["data"]) == Decimal("30")

    ranged = await client.get("/api/v1/cashflow/by-category",
                              params={"from": "2026-09", "to": "2026-10"}, headers=AUTH_HEADERS)
    assert ranged.status_code == 200, ranged.text
    assert _total(ranged.json()["data"]) == Decimal("100")


async def test_by_category_requires_scope(client: AsyncClient):
    """Neither period nor from/to → 400, not an unscoped full-table scan."""
    resp = await client.get("/api/v1/cashflow/by-category", headers=AUTH_HEADERS)
    assert resp.status_code == 400, resp.text


pytestmark = pytest.mark.asyncio
