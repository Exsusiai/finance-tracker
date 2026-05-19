"""P1.4: crypto-account balance = SUM(holding.quantity × latest price).

A crypto_wallet / exchange account has zero transactions, so the
`v_account_balance` SQL view always reports 0. The real worth of these
accounts is on the asset_holdings rows. This module owns that math —
a small helper used by the /balances endpoint.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
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
from app.services.wallet_sync.holdings_value import (  # noqa: E402
    compute_holdings_value_per_account,
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


def _utcnow() -> str:
    return _utcnow_str()


async def _wallet(db: AsyncSession, name: str = "W") -> Account:
    a = Account(
        name=name, type="crypto_wallet", currency="USDT",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(a)
    await db.commit()
    return a


async def _asset(db: AsyncSession, symbol: str) -> Asset:
    a = Asset(
        symbol=symbol, name=symbol, asset_class="crypto", currency="USDT",
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(a)
    await db.flush()
    return a


async def _price(db: AsyncSession, asset: Asset, value: str, at: str | None = None):
    db.add(MarketPrice(
        asset_id=asset.id,
        quoted_at=at or _utcnow(),
        price=Decimal(value),
        currency="USDT",
        source="test",
    ))
    await db.flush()


async def _hold(db: AsyncSession, account: Account, asset: Asset, qty: str,
                chain: str = "ethereum", is_active: bool = True):
    db.add(AssetHolding(
        account_id=account.id, asset_id=asset.id, chain=chain,
        quantity=Decimal(qty), is_active=is_active,
        created_at=_utcnow(), updated_at=_utcnow(),
    ))
    await db.flush()


class TestComputeHoldingsValue:
    async def test_empty_db_returns_empty_map(self, db: AsyncSession):
        assert await compute_holdings_value_per_account(db) == {}

    async def test_single_holding(self, db: AsyncSession):
        acc = await _wallet(db, "Single")
        eth = await _asset(db, "ETH")
        await _price(db, eth, "3200")
        await _hold(db, acc, eth, "0.5")
        await db.commit()

        result = await compute_holdings_value_per_account(db)
        assert result[acc.id] == Decimal("1600")  # 0.5 * 3200

    async def test_multi_holding_same_account(self, db: AsyncSession):
        acc = await _wallet(db, "Multi")
        eth = await _asset(db, "ETH-multi")
        usdc = await _asset(db, "USDC-multi")
        await _price(db, eth, "3000")
        await _price(db, usdc, "1.0001")
        await _hold(db, acc, eth, "2", chain="ethereum")
        await _hold(db, acc, usdc, "1000", chain="arbitrum")
        await db.commit()

        result = await compute_holdings_value_per_account(db)
        # 2 * 3000 + 1000 * 1.0001 = 6000 + 1000.1 = 7000.1
        assert result[acc.id] == Decimal("7000.1000")

    async def test_inactive_holding_excluded(self, db: AsyncSession):
        """is_active=False = disappeared-from-chain holdings are NOT counted."""
        acc = await _wallet(db, "Inactive")
        eth = await _asset(db, "ETH-inactive")
        await _price(db, eth, "3000")
        await _hold(db, acc, eth, "1", is_active=False)
        await db.commit()

        result = await compute_holdings_value_per_account(db)
        assert acc.id not in result  # no rows → no entry at all

    async def test_holding_without_price_omitted(self, db: AsyncSession):
        """Unpriced assets (CoinGecko didn't know it) contribute 0 — but
        the account still appears in the result if it has at least one
        priced holding."""
        acc = await _wallet(db, "MixedPrice")
        eth = await _asset(db, "ETH-mixedprice")
        unk = await _asset(db, "UNK-mixedprice")
        await _price(db, eth, "3000")
        # No price for `unk`.
        await _hold(db, acc, eth, "1")
        await _hold(db, acc, unk, "5")
        await db.commit()

        result = await compute_holdings_value_per_account(db)
        assert result[acc.id] == Decimal("3000")

    async def test_latest_price_wins(self, db: AsyncSession):
        """Multiple price rows for the same asset → only the newest one
        gets used in the valuation."""
        acc = await _wallet(db, "LatestPrice")
        eth = await _asset(db, "ETH-latestprice")
        await _price(db, eth, "1000", at="2024-01-01T00:00:00Z")
        await _price(db, eth, "3500", at="2026-05-01T00:00:00Z")  # newest
        await _hold(db, acc, eth, "1")
        await db.commit()

        result = await compute_holdings_value_per_account(db)
        assert result[acc.id] == Decimal("3500")

    async def test_filter_by_account_id(self, db: AsyncSession):
        """When account_ids is set, only those accounts are computed."""
        acc1 = await _wallet(db, "A1")
        acc2 = await _wallet(db, "A2")
        eth = await _asset(db, "ETH-filter")
        await _price(db, eth, "3000")
        await _hold(db, acc1, eth, "1")
        await _hold(db, acc2, eth, "2")
        await db.commit()

        result = await compute_holdings_value_per_account(db, account_ids=[acc1.id])
        assert result == {acc1.id: Decimal("3000")}
