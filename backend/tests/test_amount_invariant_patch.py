"""Tests for amount-sign invariant on PATCH and inbox confirm paths.

Sprint 3 FIX-15 (review V2 closes V1 P1-5 partial). Verifies that the two
write paths that previously bypassed `ingestion.normalize` now also enforce
ABS(amount) for non-adjustment rows.
"""

from __future__ import annotations

import os
from decimal import Decimal

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

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def override_get_db():
    async with _TestingSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}", "Content-Type": "application/json"}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_schema():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with _TestingSessionLocal() as s:
        s.add(
            Account(
                id=1,
                name="Checking",
                type="bank",
                currency="CNY",
                initial_balance=Decimal("0"),
                is_active=True,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        s.add(
            Category(
                id=1,
                name="Groceries",
                kind="expense",
                is_system=False,
                sort_order=0,
                created_at="2026-05-01T00:00:00Z",
            )
        )
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


@pytest.mark.asyncio
async def test_patch_negative_amount_normalised_to_positive(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": 1,
            "occurred_at": "2026-05-10T00:00:00Z",
            "amount": "12.00",
            "currency": "CNY",
            "type": "expense",
        },
        headers=AUTH,
    )
    assert create.status_code == 201, create.text
    tx_id = create.json()["data"]["id"]

    # Try to PATCH a negative amount; FIX-15 must store ABS.
    r = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"amount": "-50.00"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["amount"] in ("50", "50.00", "50.0"), (
        f"PATCH must normalise negative expense amount to ABS, got {r.json()['data']['amount']!r}"
    )


@pytest.mark.asyncio
async def test_patch_adjustment_keeps_sign(client: AsyncClient) -> None:
    # Adjustment rows must NOT be force-positive — the sign carries the delta.
    create = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": 1,
            "occurred_at": "2026-05-12T00:00:00Z",
            "amount": "10.00",
            "currency": "CNY",
            "type": "adjustment",
        },
        headers=AUTH,
    )
    assert create.status_code == 201, create.text
    tx_id = create.json()["data"]["id"]

    r = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"amount": "-30.00"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["data"]["amount"] in ("-30", "-30.00", "-30.0"), (
        f"adjustment must preserve signed delta, got {r.json()['data']['amount']!r}"
    )


@pytest.mark.asyncio
async def test_inbox_confirm_negative_amount_normalised(client: AsyncClient) -> None:
    # Create a pending tx via the manual API (set is_pending=True).
    create = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": 1,
            "occurred_at": "2026-05-15T00:00:00Z",
            "amount": "20.00",
            "currency": "CNY",
            "type": "expense",
            "is_pending": True,
        },
        headers=AUTH,
    )
    assert create.status_code == 201, create.text
    tx_id = create.json()["data"]["id"]

    # Confirm via /inbox while overriding amount to a signed value.
    r = await client.post(
        f"/api/v1/transactions/inbox/{tx_id}/confirm",
        json={"amount": "-77.50", "category_id": 1},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["amount"] in ("77.5", "77.50"), (
        f"inbox confirm must normalise expense to ABS, got {r.json()['data']['amount']!r}"
    )
