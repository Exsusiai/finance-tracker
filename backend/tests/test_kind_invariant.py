"""Tests for category-kind ↔ transaction-type invariant (Sprint 1 FIX-5,
review V1 §P1-4).

Scenarios:
- POST /transactions with mismatched (type, category.kind) → 422.
- PATCH /transactions/{id} flipping type to mismatch the existing category → 422.
- POST /categories with parent_id of a different kind → 422.
- POST /categories with non-existent parent_id → 422.
- Happy path: matched kind → 201/200.
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
    # Save & restore the dependency override so concurrent test modules don't
    # leak each other's in-memory engines (Sprint 1 FIX-7).
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed: one account + one expense category + one income category
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
                id=1001,
                name="Groceries",
                kind="expense",
                is_system=False,
                sort_order=0,
                created_at="2026-05-01T00:00:00Z",
            )
        )
        s.add(
            Category(
                id=1002,
                name="Salary",
                kind="income",
                is_system=False,
                sort_order=0,
                created_at="2026-05-01T00:00:00Z",
            )
        )
        await s.commit()
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    # Restore so other test modules see their own override.
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── POST /transactions ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_with_mismatched_kind_returns_422(client: AsyncClient) -> None:
    body = {
        "account_id": 1,
        "occurred_at": "2026-05-10T00:00:00Z",
        "amount": "50.00",
        "currency": "CNY",
        "type": "income",  # but category 1001 is `expense` kind
        "category_id": 1001,
        "description": "wrong kind",
    }
    r = await client.post("/api/v1/transactions", json=body, headers=AUTH)
    assert r.status_code == 422
    body_json = r.json()
    assert body_json["error"]["code"] == "INVALID_INPUT"
    assert "kind" in body_json["error"]["message"].lower()


@pytest.mark.asyncio
async def test_create_with_matched_kind_succeeds(client: AsyncClient) -> None:
    body = {
        "account_id": 1,
        "occurred_at": "2026-05-10T00:00:00Z",
        "amount": "12.50",
        "currency": "CNY",
        "type": "expense",
        "category_id": 1001,  # expense kind ✓
        "description": "groceries",
    }
    r = await client.post("/api/v1/transactions", json=body, headers=AUTH)
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_patch_flipping_type_mismatch_returns_422(client: AsyncClient) -> None:
    # Create a clean expense row first
    create = await client.post(
        "/api/v1/transactions",
        json={
            "account_id": 1,
            "occurred_at": "2026-05-11T00:00:00Z",
            "amount": "8.00",
            "currency": "CNY",
            "type": "expense",
            "category_id": 1001,
            "description": "patch-target",
        },
        headers=AUTH,
    )
    assert create.status_code == 201
    tx_id = create.json()["data"]["id"]

    # Now flip type to income while keeping the expense category → must reject
    r = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"type": "income"},
        headers=AUTH,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_INPUT"


# ─── POST /categories ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_subcategory_kind_mismatch_returns_422(client: AsyncClient) -> None:
    body = {
        "name": "Coffee",
        "kind": "income",  # parent is `expense` kind → mismatch
        "parent_id": 1001,
    }
    r = await client.post("/api/v1/categories", json=body, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_create_subcategory_with_unknown_parent_returns_422(client: AsyncClient) -> None:
    body = {"name": "Lost", "kind": "expense", "parent_id": 9999}
    r = await client.post("/api/v1/categories", json=body, headers=AUTH)
    assert r.status_code == 422
    assert "does not exist" in r.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_create_subcategory_matching_kind_succeeds(client: AsyncClient) -> None:
    body = {"name": "Dining", "kind": "expense", "parent_id": 1001}
    r = await client.post("/api/v1/categories", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
