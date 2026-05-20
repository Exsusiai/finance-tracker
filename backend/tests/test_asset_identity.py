"""A1: Asset identity by (asset_class, symbol, chain, contract).

Verifies the schema change that splits crypto Asset rows per chain +
contract. Before this change, USDT-on-Ethereum and USDT-on-Arbitrum
shared one Asset row (and one price), so a chain-specific contract
price could be silently mapped onto a different chain's holdings.

Tests written BEFORE implementation lands (RED → GREEN).
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base  # noqa: E402
from app.models import Asset  # noqa: E402


TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(
    _engine, expire_on_commit=False, class_=AsyncSession
)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _TestingSessionLocal() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


def _utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _asset(
    symbol: str,
    *,
    chain: str = "",
    contract: str = "",
    asset_class: str = "crypto",
    currency: str = "USDT",
    is_active: bool = True,
) -> Asset:
    return Asset(
        symbol=symbol,
        name=symbol,
        asset_class=asset_class,
        currency=currency,
        chain=chain,
        contract=contract,
        is_active=is_active,
        created_at=_utc(),
        updated_at=_utc(),
    )


# ─── New columns exist with sensible defaults ────────────────────────────


class TestNewColumns:
    async def test_chain_defaults_to_empty(self, db: AsyncSession):
        a = Asset(
            symbol="BTC", name="Bitcoin", asset_class="crypto",
            currency="USDT", created_at=_utc(), updated_at=_utc(),
        )
        db.add(a)
        await db.flush()
        assert a.chain == ""
        assert a.contract == ""

    async def test_is_active_defaults_to_true(self, db: AsyncSession):
        a = Asset(
            symbol="ETH-legacy", name="ETH", asset_class="crypto",
            currency="USDT", created_at=_utc(), updated_at=_utc(),
        )
        db.add(a)
        await db.flush()
        assert a.is_active is True


# ─── Unique constraint behavior ──────────────────────────────────────────


class TestUniqueIdentity:
    async def test_same_symbol_different_chain_coexist(self, db: AsyncSession):
        """USDT on Ethereum and USDT on Arbitrum must live in distinct rows."""
        db.add_all([
            _asset("USDT", chain="ethereum",
                   contract="0xdac17f958d2ee523a2206206994597c13d831ec7"),
            _asset("USDT", chain="arbitrum",
                   contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"),
        ])
        await db.commit()  # must not raise

    async def test_same_symbol_same_chain_different_contract_coexist(
        self, db: AsyncSession
    ):
        """Two legitimately different contracts on the same chain
        (e.g. USDC native vs USDC.E bridged) — both 'USDC' symbol on
        Arbitrum but distinct contracts. They should be two rows."""
        db.add_all([
            _asset("USDC", chain="arbitrum",
                   contract="0xaf88d065e77c8cc2239327c5edb3a432268e5831"),
            _asset("USDC", chain="arbitrum",
                   contract="0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"),
        ])
        await db.commit()

    async def test_duplicate_full_identity_blocked(self, db: AsyncSession):
        """Same (symbol, asset_class, chain, contract) → IntegrityError."""
        db.add(_asset("WBTC", chain="ethereum",
                      contract="0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"))
        await db.commit()

        async with _TestingSessionLocal() as s2:
            await s2.execute(text("PRAGMA foreign_keys=ON"))
            s2.add(_asset("WBTC", chain="ethereum",
                          contract="0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"))
            with pytest.raises(IntegrityError):
                await s2.commit()

    async def test_native_coin_single_row_for_all_chains(self, db: AsyncSession):
        """ETH appears on Ethereum L1 AND on Arbitrum L2 as native gas
        — by design (decision #1) we keep ONE Asset row for ETH with
        chain='' and rely on the holding row's `chain` to discriminate
        position. Trying to insert a second ETH row should be blocked."""
        db.add(_asset("ETH", chain="", contract=""))
        await db.commit()

        async with _TestingSessionLocal() as s2:
            await s2.execute(text("PRAGMA foreign_keys=ON"))
            s2.add(_asset("ETH", chain="", contract=""))
            with pytest.raises(IntegrityError):
                await s2.commit()

    async def test_legacy_manual_entry_distinct_from_onchain(
        self, db: AsyncSession
    ):
        """A user-typed 'USDT' (chain='', contract='') vs a sync-created
        USDT-ethereum row must coexist — `chain=''` denotes legacy /
        manual / CEX-pooled, not 'matches every chain'."""
        db.add_all([
            _asset("USDT-manual", chain="", contract=""),
            _asset("USDT-manual", chain="ethereum",
                   contract="0xdac17f958d2ee523a2206206994597c13d831ec7"),
        ])
        await db.commit()

    async def test_cross_asset_class_does_not_collide(self, db: AsyncSession):
        """A cash 'USDT' (asset_class='cash') row and a crypto 'USDT'
        (asset_class='crypto') row must coexist — asset_class is part
        of the identity."""
        db.add_all([
            Asset(symbol="X", name="X cash", asset_class="cash",
                  currency="USD", created_at=_utc(), updated_at=_utc()),
            Asset(symbol="X", name="X crypto", asset_class="crypto",
                  currency="USDT", created_at=_utc(), updated_at=_utc()),
        ])
        await db.commit()


# ─── is_active soft-archive ──────────────────────────────────────────────


class TestSoftArchive:
    async def test_archived_asset_can_coexist_with_replacement(
        self, db: AsyncSession
    ):
        """When A3 migration archives an old shared 'DAI' Asset and
        creates a new chain-specific one, the archived row stays in
        place with is_active=False. Both rows must coexist as long as
        they differ on (chain, contract)."""
        db.add(_asset("DAI", chain="", contract="", is_active=False))
        db.add(_asset("DAI", chain="ethereum",
                      contract="0x6b175474e89094c44da98b954eedeac495271d0f",
                      is_active=True))
        await db.commit()
