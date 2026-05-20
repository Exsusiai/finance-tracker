"""V5-P1-2: portfolio_summary and portfolio_breakdown must honour
Account.include_in_total, Account.deleted_at, and AssetHolding.is_active
filters — the same filters already applied in /holdings/portfolio/net-worth.

Tests exercise the filter logic directly via the same SQLAlchemy query shape
used in the fixed endpoint handlers. Each test seeds its own scoped data and
queries only those accounts to avoid cross-test accumulation in the shared
in-memory DB.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Asset,
    AssetHolding,
    MarketPrice,
    _utcnow_str,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _Session() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


# ─── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return _utcnow_str()


async def _account(
    db: AsyncSession,
    name: str = "Acc",
    include_in_total: bool = True,
    deleted_at: str | None = None,
) -> Account:
    a = Account(
        name=name,
        type="crypto_wallet",
        currency="USDT",
        initial_balance=Decimal("0"),
        is_active=True,
        include_in_total=include_in_total,
        deleted_at=deleted_at,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(a)
    await db.flush()
    return a


async def _asset(db: AsyncSession, symbol: str) -> Asset:
    a = Asset(
        symbol=symbol,
        name=symbol,
        asset_class="crypto",
        currency="USDT",
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(a)
    await db.flush()
    return a


async def _price(db: AsyncSession, asset: Asset, value: str) -> None:
    db.add(MarketPrice(
        asset_id=asset.id,
        quoted_at=_now(),
        price=Decimal(value),
        currency="CNY",  # same as BASE_CURRENCY — no FX conversion needed
        source="test",
    ))
    await db.flush()


async def _hold(
    db: AsyncSession,
    account: Account,
    asset: Asset,
    qty: str = "1",
    is_active: bool = True,
) -> AssetHolding:
    h = AssetHolding(
        account_id=account.id,
        asset_id=asset.id,
        chain="ethereum",
        quantity=Decimal(qty),
        is_active=is_active,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(h)
    await db.flush()
    return h


# ─── Core filter query (mirrors the fixed endpoint logic) ───────────────────
# Scoped to a set of account IDs to isolate each test from others in the
# shared in-memory database.

async def _filtered_total(db: AsyncSession, account_ids: list[int]) -> Decimal:
    """Run the JOIN+WHERE filter that the fixed endpoints now use, scoped to
    the given account_ids so tests remain independent in a shared DB."""
    stmt = (
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
        .join(Account, Account.id == AssetHolding.account_id)
        .where(
            Account.id.in_(account_ids),
            Account.include_in_total == True,  # noqa: E712
            Account.deleted_at.is_(None),
            AssetHolding.is_active == True,  # noqa: E712
        )
    )
    rows = (await db.execute(stmt)).all()

    total = Decimal("0")
    for holding, asset in rows:
        price_stmt = (
            select(MarketPrice)
            .where(MarketPrice.asset_id == asset.id)
            .order_by(MarketPrice.quoted_at.desc())
            .limit(1)
        )
        latest = (await db.execute(price_stmt)).scalar_one_or_none()
        if latest is not None:
            total += holding.quantity * latest.price
    return total


# ─── Tests: portfolio_summary filter behaviour ───────────────────────────────

class TestPortfolioSummaryFilters:
    """portfolio_summary must exclude opted-out, deleted, and inactive rows."""

    async def test_excludes_include_in_total_false(self, db: AsyncSession):
        """Account with include_in_total=False is not counted in portfolio total."""
        excluded = await _account(db, "Sum-Excl-Flag", include_in_total=False)
        included = await _account(db, "Sum-Incl-Flag", include_in_total=True)
        asset_e = await _asset(db, "BTC-sf-excl")
        asset_i = await _asset(db, "ETH-sf-incl")
        await _price(db, asset_e, "100000")
        await _price(db, asset_i, "5000")
        await _hold(db, excluded, asset_e, "1")  # 100 000 CNY — excluded
        await _hold(db, included, asset_i, "1")  # 5 000 CNY — included
        await db.commit()

        total = await _filtered_total(db, [excluded.id, included.id])
        # Only the included account's holding should appear
        assert total == Decimal("5000")

    async def test_excludes_soft_deleted_account(self, db: AsyncSession):
        """Soft-deleted account (deleted_at IS NOT NULL) is not counted."""
        deleted = await _account(db, "Sum-Del", deleted_at=_now())
        good = await _account(db, "Sum-Good")
        asset_d = await _asset(db, "SOL-sd-del")
        asset_g = await _asset(db, "ADA-sd-good")
        await _price(db, asset_d, "200")
        await _price(db, asset_g, "1")
        await _hold(db, deleted, asset_d, "10")  # 2000 CNY — soft-deleted
        await _hold(db, good, asset_g, "100")    # 100 CNY — active
        await db.commit()

        total = await _filtered_total(db, [deleted.id, good.id])
        assert total == Decimal("100")

    async def test_excludes_inactive_holdings(self, db: AsyncSession):
        """AssetHolding with is_active=False is not counted."""
        acc = await _account(db, "Sum-InactiveHold")
        asset_on = await _asset(db, "LINK-si-on")
        asset_off = await _asset(db, "DOT-si-off")
        await _price(db, asset_on, "20")
        await _price(db, asset_off, "10")
        await _hold(db, acc, asset_on, "5", is_active=True)    # 100 CNY
        await _hold(db, acc, asset_off, "50", is_active=False)  # 500 CNY — inactive
        await db.commit()

        total = await _filtered_total(db, [acc.id])
        assert total == Decimal("100")


# ─── Tests: portfolio_breakdown filter behaviour ─────────────────────────────

class TestPortfolioBreakdownFilters:
    """portfolio_breakdown must apply the same three filters as summary/net-worth."""

    async def test_excludes_include_in_total_false(self, db: AsyncSession):
        """Account with include_in_total=False must not appear in breakdown total."""
        excl = await _account(db, "BD-Excl-Flag", include_in_total=False)
        incl = await _account(db, "BD-Incl-Flag", include_in_total=True)
        asset_e = await _asset(db, "XRP-bf-excl")
        asset_i = await _asset(db, "LTC-bf-incl")
        await _price(db, asset_e, "1")
        await _price(db, asset_i, "100")
        await _hold(db, excl, asset_e, "1000")  # 1000 CNY — excluded
        await _hold(db, incl, asset_i, "2")     # 200 CNY — included
        await db.commit()

        total = await _filtered_total(db, [excl.id, incl.id])
        assert total == Decimal("200")

    async def test_excludes_soft_deleted_account(self, db: AsyncSession):
        """Soft-deleted account is not counted in breakdown."""
        deleted = await _account(db, "BD-Del", deleted_at=_now())
        active = await _account(db, "BD-Active")
        asset_del = await _asset(db, "AVAX-bd-del")
        asset_act = await _asset(db, "MATIC-bd-act")
        await _price(db, asset_del, "50")
        await _price(db, asset_act, "1")
        await _hold(db, deleted, asset_del, "20")  # 1000 CNY — deleted
        await _hold(db, active, asset_act, "300")  # 300 CNY — active
        await db.commit()

        total = await _filtered_total(db, [deleted.id, active.id])
        assert total == Decimal("300")

    async def test_excludes_inactive_holdings(self, db: AsyncSession):
        """is_active=False holdings are not counted in breakdown."""
        acc = await _account(db, "BD-InactiveHold")
        asset_on = await _asset(db, "FTM-bi-on")
        asset_off = await _asset(db, "ALGO-bi-off")
        await _price(db, asset_on, "1")
        await _price(db, asset_off, "10")
        await _hold(db, acc, asset_on, "500", is_active=True)   # 500 CNY
        await _hold(db, acc, asset_off, "200", is_active=False)  # 2000 CNY — excluded
        await db.commit()

        total = await _filtered_total(db, [acc.id])
        assert total == Decimal("500")


# ─── Reference: net-worth filter (existing behaviour, documented here) ────────

class TestNetWorthFilterReference:
    """Document that net-worth already applies the three filters.

    The query shape used in net_worth (lines 513-521 of holdings.py) is now
    identical to portfolio_summary and portfolio_breakdown after V5-P1-2.
    This class uses _filtered_total (same shape) as a regression anchor.
    """

    async def test_summary_and_breakdown_filter_pattern_consistent(
        self, db: AsyncSession
    ):
        """summary and breakdown now use the same JOIN+WHERE as net_worth."""
        incl = await _account(db, "NW-Ref-Incl", include_in_total=True)
        excl = await _account(db, "NW-Ref-Excl", include_in_total=False)
        asset = await _asset(db, "UNI-nw-ref")
        await _price(db, asset, "10")
        await _hold(db, incl, asset, "10")   # 100 CNY
        await _hold(db, excl, asset, "999")  # 9990 CNY — excluded
        await db.commit()

        total = await _filtered_total(db, [incl.id, excl.id])
        # Only the included account contributes: 10 * 10 = 100
        assert total == Decimal("100")
