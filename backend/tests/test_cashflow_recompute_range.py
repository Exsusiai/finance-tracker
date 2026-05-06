"""Tests for recompute_cashflow cross-year range fix.

Sprint 4 FIX-21 (review V3 §V3-P1-4):
- `POST /cashflow/recompute?from=2025-12&to=2026-02` previously filtered
  month>=12 AND month<=2 (always empty). Now uses string comparison on
  substr(occurred_at, 1, 7).

Cases:
1. cross-year range — tx in 2025-12 and 2026-02; recompute covers both.
2. single month — only that month updated.
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

_TEST_TOKEN = "deadc0dedeadc0dedeadc0dedeadc0dedeadc0de"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, CashFlowSnapshot  # noqa: E402
from sqlalchemy import select  # noqa: E402

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
            id=20,
            name="Range Test",
            type="bank",
            currency="CNY",
            initial_balance=Decimal("0"),
            is_active=True,
            created_at="2025-11-01T00:00:00Z",
            updated_at="2025-11-01T00:00:00Z",
        ))
        s.add(Category(
            id=3001,
            name="Bills",
            kind="expense",
            is_system=False,
            sort_order=0,
            created_at="2025-11-01T00:00:00Z",
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


# ─── Case 1: cross-year range covers both months ─────────────────────────────

@pytest.mark.asyncio
async def test_cross_year_recompute_covers_both_months(client: AsyncClient) -> None:
    """Recompute from=2025-12 to=2026-02 should include both boundary months."""
    # Insert one tx per boundary month
    for occurred_at, desc in [
        ("2025-12-15T00:00:00Z", "dec tx"),
        ("2026-02-10T00:00:00Z", "feb tx"),
    ]:
        r = await client.post("/api/v1/transactions", json={
            "account_id": 20,
            "occurred_at": occurred_at,
            "amount": "50",
            "currency": "CNY",
            "type": "expense",
            "description": desc,
        }, headers=AUTH)
        assert r.status_code == 201, r.text

    # Trigger recompute with canonical params
    r = await client.post("/api/v1/cashflow/recompute?from=2025-12&to=2026-02", headers=AUTH)
    assert r.status_code == 200, r.text

    # Check snapshots exist for both months
    async with _TestingSessionLocal() as s:
        rows = (await s.execute(
            select(CashFlowSnapshot)
            .where(
                CashFlowSnapshot.period_year.in_([2025, 2026]),
                CashFlowSnapshot.period_month.in_([12, 2]),
            )
            .order_by(CashFlowSnapshot.period_year, CashFlowSnapshot.period_month)
        )).scalars().all()

    periods = {(r.period_year, r.period_month) for r in rows}
    assert (2025, 12) in periods, f"Expected 2025-12 snapshot, got {periods}"
    assert (2026, 2) in periods, f"Expected 2026-02 snapshot, got {periods}"


# ─── Case 2: single-month recompute only touches that month ─────────────────

@pytest.mark.asyncio
async def test_single_month_recompute(client: AsyncClient) -> None:
    """Recompute from=2026-01&to=2026-01 should create/update only 2026-01."""
    r = await client.post("/api/v1/transactions", json={
        "account_id": 20,
        "occurred_at": "2026-01-20T00:00:00Z",
        "amount": "75",
        "currency": "CNY",
        "type": "expense",
        "description": "jan tx",
    }, headers=AUTH)
    assert r.status_code == 201, r.text

    r = await client.post("/api/v1/cashflow/recompute?from=2026-01&to=2026-01", headers=AUTH)
    assert r.status_code == 200, r.text

    async with _TestingSessionLocal() as s:
        snap = (await s.execute(
            select(CashFlowSnapshot)
            .where(
                CashFlowSnapshot.period_year == 2026,
                CashFlowSnapshot.period_month == 1,
            )
        )).scalar_one_or_none()

    assert snap is not None, "Snapshot for 2026-01 should exist"
    assert snap.expense_total >= Decimal("75")


# ─── Case 3: legacy params still work ────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_params_still_accepted(client: AsyncClient) -> None:
    """Legacy from_year/from_month/to_year/to_month params should still work."""
    r = await client.post(
        "/api/v1/cashflow/recompute"
        "?from_year=2026&from_month=1&to_year=2026&to_month=1",
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "recomputed"
