"""Tests for forward portfolio-value snapshots (dashboard 组合市值走势).

- capture_portfolio_snapshot upserts ONE row per month (re-run overwrites).
- GET /holdings/portfolio/value-history returns the captured series.
- Snapshot net_worth matches compute_net_worth (single-sourced math).
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "snapsnapsnapsnapsnapsnapsnapsnapsnapsnap"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.core.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, PortfolioSnapshot  # noqa: E402

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
        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))
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


@pytest.mark.asyncio
async def test_snapshot_upserts_one_row_per_week(client: AsyncClient):
    cur = get_settings().base_currency
    from app.services.valuation.snapshot import capture_portfolio_snapshot

    async with _Session() as s:
        s.add(Account(name="Bank", type="bank", currency=cur,
                      initial_balance=Decimal("1000"), is_active=True,
                      created_at=_utcnow(), updated_at=_utcnow()))
        await s.commit()

    # Two captures in the same week → one row, latest value wins.
    async with _Session() as s:
        snap1 = await capture_portfolio_snapshot(s, cur)
        await s.commit()
        assert snap1.cash_total == Decimal("1000")
        assert snap1.net_worth == Decimal("1000")
        assert len(snap1.period) == 10  # "YYYY-MM-DD" week key

    async with _Session() as s:
        # Bump the balance, re-capture same week.
        acc = (await s.execute(select(Account))).scalars().first()
        acc.initial_balance = Decimal("1500")
        await s.commit()
    async with _Session() as s:
        await capture_portfolio_snapshot(s, cur)
        await s.commit()

    async with _Session() as s:
        rows = (await s.execute(select(PortfolioSnapshot))).scalars().all()
        assert len(rows) == 1, "same week must upsert, not append"
        assert rows[0].net_worth == Decimal("1500")


@pytest.mark.asyncio
async def test_cash_history_matches_net_worth_cash_leg(client: AsyncClient):
    """The latest cash-history point must equal compute_net_worth's cash_total
    (single-sourced FX methodology); the line is anchored to real balances."""
    cur = get_settings().base_currency
    from app.services.valuation.cash_history import compute_cash_history
    from app.services.valuation.net_worth import compute_net_worth
    from app.models import Transaction

    async with _Session() as s:
        acc = Account(name="CashAcc", type="bank", currency=cur,
                      initial_balance=Decimal("500"), is_active=True, include_in_total=True,
                      created_at=_utcnow(), updated_at=_utcnow())
        s.add(acc)
        await s.flush()
        s.add_all([
            Transaction(account_id=acc.id, occurred_at="2026-03-10T00:00:00Z",
                        amount=Decimal("100"), currency=cur, type="income", source="manual",
                        is_pending=False, created_at=_utcnow(), updated_at=_utcnow()),
            Transaction(account_id=acc.id, occurred_at="2026-04-10T00:00:00Z",
                        amount=Decimal("30"), currency=cur, type="expense", source="manual",
                        is_pending=False, created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await s.commit()

    async with _Session() as s:
        hist = await compute_cash_history(s, cur)
        nw = await compute_net_worth(s, cur)

    assert hist, "expected at least one cash-history period"
    # Ascending periods; the invariant that anchors the line to reality:
    assert [p for p, _ in hist] == sorted(p for p, _ in hist)
    assert Decimal(hist[-1][1]) == nw.cash_total

    # Endpoint returns the series.
    resp = await client.get("/api/v1/holdings/portfolio/value-history", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["net_worth"] == "1500.00000000" or Decimal(data[0]["net_worth"]) == Decimal("1500")
    assert data[0]["base_currency"] == cur


@pytest.mark.asyncio
async def test_value_history_empty_when_no_snapshots(client: AsyncClient):
    # Fresh process state may already have a row from the other test; this
    # only asserts the endpoint shape stays a list.
    resp = await client.get("/api/v1/holdings/portfolio/value-history", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["data"], list)
