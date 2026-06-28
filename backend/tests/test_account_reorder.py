"""Tests for manual account reordering (drag-to-reorder, 2026-06-27).

PATCH /accounts/reorder persists the given id order as sort_order; the
accounts list is then returned in that order. Unknown ids are rejected.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "reorderreorderreorderreorderreorderre"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
# Reorder is currency-agnostic. Use the project-default CNY so this
# alphabetically-first test file doesn't change the process-wide
# BASE_CURRENCY that currency-sensitive suites (e.g.
# test_multi_currency_ingestion) rely on via os.environ.setdefault.
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account  # noqa: E402

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}", "Content-Type": "application/json"}


async def _override_get_db():
    async with _Session() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _tables():
    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.pop(get_db, None) if prev is None else app.dependency_overrides.__setitem__(get_db, prev)


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed(n: int) -> list[int]:
    async with _Session() as s:
        accts = [
            Account(name=f"Acc{i}", type="bank", currency="EUR",
                    initial_balance=Decimal("0"), is_active=True,
                    created_at=_utcnow(), updated_at=_utcnow())
            for i in range(n)
        ]
        s.add_all(accts)
        await s.commit()
        return [a.id for a in accts]


@pytest.mark.asyncio
async def test_reorder_persists_order(client: AsyncClient):
    ids = await _seed(3)
    reversed_ids = list(reversed(ids))

    resp = await client.patch("/api/v1/accounts/reorder", headers=AUTH,
                              json={"account_ids": reversed_ids})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["reordered"] == 3

    listed = await client.get("/api/v1/accounts", headers=AUTH)
    got = [a["id"] for a in listed.json()["data"]]
    assert got == reversed_ids
    # sort_order is the index in the new order.
    out = {a["id"]: a["sort_order"] for a in listed.json()["data"]}
    assert [out[i] for i in reversed_ids] == [0, 1, 2]


@pytest.mark.asyncio
async def test_reorder_rejects_unknown_id(client: AsyncClient):
    ids = await _seed(2)
    resp = await client.patch("/api/v1/accounts/reorder", headers=AUTH,
                              json={"account_ids": [*ids, 999999]})
    assert resp.status_code == 400, resp.text
    assert "999999" in resp.json()["detail"]
