"""Tests for base_amount lifecycle and FX-missing handling.

Sprint 4 FIX-19 (review V3 §V3-P0-1 + §V3-P0-2):
- Cashflow no longer falls back to raw `amount` when FX is missing.
- PATCH clears stale base_amount and re-folds via ingestion pipeline.

Cases:
1. same-currency unchanged — base=CNY, 100 CNY tx, cashflow reads 100.
2. fx missing excluded — base=CNY, 100 GBP tx (no fx seeded), excluded; fx_missing_count==1.
3. fx with rate folds — seed EUR→CNY=8, 50 EUR tx → base_amount=400, cashflow reads 400.
4. PATCH amount refolds — 50 EUR → 400; PATCH amount=100 → base_amount should be 800.
5. PATCH currency refolds — 100 CNY tx; PATCH {currency=EUR, amount=50} with EUR→CNY=8 → base_amount=400.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "cafebabecafebabecafebabecafebabecafebabe"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, FxRate  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}", "Content-Type": "application/json"}


async def override_get_db():
    async with _TestingSessionLocal() as session:
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
    async with _TestingSessionLocal() as s:
        s.add(Account(
            id=10,
            name="Main CNY",
            type="bank",
            currency="CNY",
            initial_balance=Decimal("0"),
            is_active=True,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ))
        s.add(Category(
            id=2001,
            name="Food",
            kind="expense",
            is_system=False,
            sort_order=0,
            created_at="2026-01-01T00:00:00Z",
        ))
        # Seed EUR→CNY rate = 8
        s.add(FxRate(
            base_currency="EUR",
            quote_currency="CNY",
            rate=Decimal("8"),
            source="test",
            quoted_at="2026-01-01T00:00:00Z",
        ))
        await s.commit()
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── Case 1: same-currency row (CNY=base) ───────────────────────────────────

@pytest.mark.asyncio
async def test_same_currency_included_in_cashflow(client: AsyncClient) -> None:
    """100 CNY expense with base=CNY should appear in cashflow as 100."""
    r = await client.post("/api/v1/transactions", json={
        "account_id": 10,
        "occurred_at": "2026-03-01T00:00:00Z",
        "amount": "100",
        "currency": "CNY",
        "type": "expense",
        "description": "same-currency test",
    }, headers=AUTH)
    assert r.status_code == 201, r.text

    cf = await client.get("/api/v1/cashflow/monthly?from=2026-03&to=2026-03", headers=AUTH)
    assert cf.status_code == 200, cf.text
    months = cf.json()["data"]
    assert len(months) == 1
    assert Decimal(months[0]["expense"]) == Decimal("100")


# ─── Case 2: FX missing → excluded ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_fx_missing_excluded_from_cashflow(client: AsyncClient) -> None:
    """100 GBP expense with no FX rate seeded should be excluded and flagged."""
    r = await client.post("/api/v1/transactions", json={
        "account_id": 10,
        "occurred_at": "2026-04-01T00:00:00Z",
        "amount": "100",
        "currency": "GBP",
        "type": "expense",
        "description": "GBP no-fx test",
    }, headers=AUTH)
    assert r.status_code == 201, r.text

    cf = await client.get("/api/v1/cashflow/monthly?from=2026-04&to=2026-04", headers=AUTH)
    assert cf.status_code == 200, cf.text
    months = cf.json()["data"]
    assert len(months) == 1
    # The GBP row has no FX — should be excluded from expense total (or None SUM → 0).
    assert Decimal(months[0]["expense"]) == Decimal("0")
    assert months[0]["fx_missing_count"] >= 1


# ─── Case 3: FX with rate → folds correctly ─────────────────────────────────

@pytest.mark.asyncio
async def test_eur_tx_folds_to_base_amount(client: AsyncClient) -> None:
    """50 EUR expense with EUR→CNY=8 should fold to base_amount=400."""
    r = await client.post("/api/v1/transactions", json={
        "account_id": 10,
        "occurred_at": "2026-05-01T00:00:00Z",
        "amount": "50",
        "currency": "EUR",
        "type": "expense",
        "description": "EUR fold test",
    }, headers=AUTH)
    assert r.status_code == 201, r.text
    tx_id = r.json()["data"]["id"]

    # Verify base_amount was set during ingestion
    detail = await client.get(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["base_amount"] is not None
    assert Decimal(data["base_amount"]) == Decimal("400")

    cf = await client.get("/api/v1/cashflow/monthly?from=2026-05&to=2026-05", headers=AUTH)
    assert cf.status_code == 200, cf.text
    months = cf.json()["data"]
    assert len(months) == 1
    assert Decimal(months[0]["expense"]) == Decimal("400")


# ─── Case 4: PATCH amount refolds base_amount ───────────────────────────────

@pytest.mark.asyncio
async def test_patch_amount_refolds_base_amount(client: AsyncClient) -> None:
    """After PATCH amount=100 on 50 EUR (base=400), base_amount should update to 800."""
    # Create 50 EUR tx
    r = await client.post("/api/v1/transactions", json={
        "account_id": 10,
        "occurred_at": "2026-06-01T00:00:00Z",
        "amount": "50",
        "currency": "EUR",
        "type": "expense",
        "description": "EUR patch refold",
    }, headers=AUTH)
    assert r.status_code == 201, r.text
    tx_id = r.json()["data"]["id"]

    # Confirm initial base_amount = 400
    detail = await client.get(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    assert Decimal(detail.json()["data"]["base_amount"]) == Decimal("400")

    # PATCH amount → 100; base_amount should recompute to 800
    p = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"amount": "100"},
        headers=AUTH,
    )
    assert p.status_code == 200, p.text

    detail2 = await client.get(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    assert detail2.status_code == 200, detail2.text
    updated = detail2.json()["data"]
    assert updated["base_amount"] is not None, "base_amount should have been refolded"
    assert Decimal(updated["base_amount"]) == Decimal("800"), (
        f"Expected 800 (100 EUR * 8), got {updated['base_amount']}"
    )

    cf = await client.get("/api/v1/cashflow/monthly?from=2026-06&to=2026-06", headers=AUTH)
    months = cf.json()["data"]
    assert Decimal(months[0]["expense"]) == Decimal("800")


# ─── Case 5: PATCH currency refolds ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_currency_refolds_base_amount(client: AsyncClient) -> None:
    """100 CNY tx PATCHed to {currency=EUR, amount=50} → base_amount=400."""
    r = await client.post("/api/v1/transactions", json={
        "account_id": 10,
        "occurred_at": "2026-07-01T00:00:00Z",
        "amount": "100",
        "currency": "CNY",
        "type": "expense",
        "description": "CNY→EUR currency patch",
    }, headers=AUTH)
    assert r.status_code == 201, r.text
    tx_id = r.json()["data"]["id"]

    p = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"currency": "EUR", "amount": "50"},
        headers=AUTH,
    )
    assert p.status_code == 200, p.text

    detail = await client.get(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    updated = detail.json()["data"]
    assert updated["currency"] == "EUR"
    assert updated["base_amount"] is not None
    assert Decimal(updated["base_amount"]) == Decimal("400"), (
        f"Expected 400 (50 EUR * 8), got {updated['base_amount']}"
    )
