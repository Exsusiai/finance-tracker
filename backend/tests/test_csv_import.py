"""Tests for CSV statement import (PayPal) via /statements/upload-csv.

Focus: the row-level dedup that makes re-uploading overlapping date ranges
safe (review requirement 1b), plus the type mapping landing in the ledger.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "csvcsvcsvcsvcsvcsvcsvcsvcsvcsvcsvcsvcsvc"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Transaction  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "csv_parser" / "paypal_sample.csv"

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
# StaticPool → one shared connection so the in-memory DB persists across the
# many short-lived sessions this test opens (insert in one request, dedup-read
# in the next). Without it aiosqlite gives each connection a fresh empty DB.
_engine = create_async_engine(
    TEST_DB_URL, echo=False, poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}"}


async def _override_get_db():
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _tables():
    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy import text as _t

        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        await conn.execute(_t(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(_t(_BALANCE_VIEW_SQL))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if prev is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = prev


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_paypal_account() -> int:
    async with _Session() as s:
        acc = Account(name="PayPal", type="bank", currency="EUR",
                      initial_balance=Decimal("0"), is_active=True,
                      created_at=_utcnow(), updated_at=_utcnow())
        s.add(acc)
        await s.commit()
        return acc.id


def _files():
    return {"file": ("paypal.csv", FIXTURE.read_bytes(), "text/csv")}


@pytest.mark.asyncio
async def test_upload_imports_and_maps_types(client: AsyncClient):
    acc_id = await _make_paypal_account()
    resp = await client.post(f"/api/v1/statements/upload-csv?account_id={acc_id}",
                             headers=AUTH, files=_files())
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["detected_source"] == "paypal"
    assert data["imported"] == 8  # 8 EUR rows; 2 USD skipped by parser
    assert data["skipped_duplicate"] == 0

    async with _Session() as s:
        rows = (await s.execute(
            select(Transaction).where(Transaction.account_id == acc_id)
        )).scalars().all()
        by_ext = {r.external_id: r for r in rows}
        assert by_ext["TXNWITHDRAW01"].type == "transfer"
        assert by_ext["TXNP2PIN01"].type == "income"
        assert by_ext["TXNCHECKOUT01"].type == "expense"
        # USD rows never imported.
        assert "TXNUSDPAY01" not in by_ext


@pytest.mark.asyncio
async def test_reupload_overlap_dedups(client: AsyncClient):
    """Re-uploading the SAME file (full overlap) must import nothing new."""
    acc_id = await _make_paypal_account()
    first = await client.post(f"/api/v1/statements/upload-csv?account_id={acc_id}",
                              headers=AUTH, files=_files())
    assert first.json()["data"]["imported"] == 8

    second = await client.post(f"/api/v1/statements/upload-csv?account_id={acc_id}",
                               headers=AUTH, files=_files())
    d = second.json()["data"]
    assert d["imported"] == 0
    assert d["skipped_duplicate"] == 8

    # Still exactly 8 rows on the account — no doubles.
    async with _Session() as s:
        n = len((await s.execute(
            select(Transaction).where(Transaction.account_id == acc_id)
        )).scalars().all())
        assert n == 8
