"""P1-4 A1: schema invariants for crypto wallets + exchanges.

Verifies the data-model groundwork the rest of P1-4 sits on:

1. ``AccountType.exchange`` is accepted by the ``ck_account_type`` CHECK so
   exchange-API accounts (Binance, Bitget, …) can be created.
2. ``chain_addresses`` table stores the (chain, address) pairs that belong
   to a ``crypto_wallet`` account; ``(account_id, chain, address)`` is unique
   and rows cascade-delete with the parent account.
3. ``exchange_connections`` mirrors the ``bank_connections`` shape for
   ``type='exchange'`` accounts: encrypted API key / secret are NOT NULL and
   the row cascade-deletes; ``(account_id, exchange)`` is unique.
4. ``asset_holdings`` gains a ``chain`` column so the same token on multiple
   chains (e.g. USDT on Ethereum vs. Arbitrum vs. Tron) lives in distinct
   rows. The old ``(account_id, asset_id)`` unique is replaced by
   ``(account_id, asset_id, chain)``; non-crypto holdings use ``chain=""``.

Tests are written BEFORE the implementation lands (RED → GREEN).
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Settings validation requires a token before importing the app.
_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Asset,
    AssetHolding,
    ChainAddress,
    ExchangeConnection,
)

# ─── Test database setup ────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(
    _engine, expire_on_commit=False, class_=AsyncSession
)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    """Create all ORM tables. FKs must be ON for cascade-delete tests."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _TestingSessionLocal() as session:
        # SQLite resets PRAGMA per-connection; re-enable FKs on every session.
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


# ─── Helpers ────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(
    db: AsyncSession,
    name: str,
    acct_type: str = "crypto_wallet",
    currency: str = "USDT",
) -> Account:
    acc = Account(
        name=name,
        type=acct_type,
        currency=currency,
        initial_balance=Decimal("0"),
        is_active=True,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(acc)
    await db.flush()
    return acc


async def _make_asset(db: AsyncSession, symbol: str = "USDT") -> Asset:
    a = Asset(
        symbol=symbol,
        name=symbol,
        asset_class="crypto",
        currency="USDT",
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(a)
    await db.flush()
    return a


# ─── 1. AccountType.exchange ───────────────────────────────────────────────


class TestAccountTypeExchange:
    async def test_exchange_type_accepted(self, db: AsyncSession):
        """type='exchange' must pass ck_account_type."""
        acc = Account(
            name="Binance Spot",
            type="exchange",
            currency="USDT",
            initial_balance=Decimal("0"),
            is_active=True,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(acc)
        await db.commit()
        assert acc.id is not None

    async def test_invalid_type_rejected(self, db: AsyncSession):
        """Garbage account types must still be rejected by the CHECK."""
        acc = Account(
            name="Bogus",
            type="not_a_real_type",
            currency="USDT",
            initial_balance=Decimal("0"),
            is_active=True,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(acc)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


# ─── 2. chain_addresses ────────────────────────────────────────────────────


class TestChainAddresses:
    async def test_insert_and_read(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-A")
        ca = ChainAddress(
            account_id=acc.id,
            chain="ethereum",
            address="0x1111111111111111111111111111111111111111",
            label="primary",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(ca)
        await db.commit()
        assert ca.id is not None

    async def test_duplicate_address_same_chain_blocked(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-Dup")
        db.add(
            ChainAddress(
                account_id=acc.id,
                chain="ethereum",
                address="0x2222222222222222222222222222222222222222",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await db.commit()

        async with _TestingSessionLocal() as session2:
            await session2.execute(text("PRAGMA foreign_keys=ON"))
            session2.add(
                ChainAddress(
                    account_id=acc.id,
                    chain="ethereum",
                    address="0x2222222222222222222222222222222222222222",
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
            with pytest.raises(IntegrityError):
                await session2.commit()

    async def test_same_address_different_chain_allowed(self, db: AsyncSession):
        """Same EVM address may legitimately exist on Ethereum AND Arbitrum."""
        acc = await _make_account(db, "Wallet-MultiChain")
        addr = "0x3333333333333333333333333333333333333333"
        db.add_all(
            [
                ChainAddress(
                    account_id=acc.id,
                    chain="ethereum",
                    address=addr,
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                ),
                ChainAddress(
                    account_id=acc.id,
                    chain="arbitrum",
                    address=addr,
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                ),
            ]
        )
        await db.commit()  # must not raise

    async def test_cascade_delete_with_account(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-Cascade")
        db.add(
            ChainAddress(
                account_id=acc.id,
                chain="bitcoin",
                address="bc1qtest000000000000000000000000000000000",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await db.commit()
        acc_id = acc.id

        await db.delete(acc)
        await db.commit()

        rows = (
            await db.execute(
                text("SELECT COUNT(*) FROM chain_addresses WHERE account_id=:a"),
                {"a": acc_id},
            )
        ).scalar()
        assert rows == 0


# ─── 3. exchange_connections ───────────────────────────────────────────────


class TestExchangeConnections:
    async def test_insert_and_read(self, db: AsyncSession):
        acc = await _make_account(db, "Binance", acct_type="exchange")
        ec = ExchangeConnection(
            account_id=acc.id,
            exchange="binance",
            api_key_enc="encrypted_key_blob",
            api_secret_enc="encrypted_secret_blob",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(ec)
        await db.commit()
        assert ec.id is not None

    async def test_unknown_exchange_rejected(self, db: AsyncSession):
        acc = await _make_account(db, "Mystery", acct_type="exchange")
        db.add(
            ExchangeConnection(
                account_id=acc.id,
                exchange="not_a_real_exchange",
                api_key_enc="x",
                api_secret_enc="y",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    async def test_missing_encrypted_creds_rejected(self, db: AsyncSession):
        acc = await _make_account(db, "Bitget", acct_type="exchange")
        db.add(
            ExchangeConnection(
                account_id=acc.id,
                exchange="bitget",
                api_key_enc=None,  # type: ignore[arg-type]
                api_secret_enc="secret",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    async def test_duplicate_exchange_per_account_blocked(self, db: AsyncSession):
        """One account can only hold one connection per exchange."""
        acc = await _make_account(db, "Binance-Dup", acct_type="exchange")
        db.add(
            ExchangeConnection(
                account_id=acc.id,
                exchange="binance",
                api_key_enc="k1",
                api_secret_enc="s1",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await db.commit()

        async with _TestingSessionLocal() as session2:
            await session2.execute(text("PRAGMA foreign_keys=ON"))
            session2.add(
                ExchangeConnection(
                    account_id=acc.id,
                    exchange="binance",
                    api_key_enc="k2",
                    api_secret_enc="s2",
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
            with pytest.raises(IntegrityError):
                await session2.commit()

    async def test_cascade_delete_with_account(self, db: AsyncSession):
        acc = await _make_account(db, "Bitget-Cascade", acct_type="exchange")
        db.add(
            ExchangeConnection(
                account_id=acc.id,
                exchange="bitget",
                api_key_enc="k",
                api_secret_enc="s",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await db.commit()
        acc_id = acc.id

        await db.delete(acc)
        await db.commit()

        rows = (
            await db.execute(
                text("SELECT COUNT(*) FROM exchange_connections WHERE account_id=:a"),
                {"a": acc_id},
            )
        ).scalar()
        assert rows == 0


# ─── 4. asset_holdings.chain ───────────────────────────────────────────────


class TestAssetHoldingChain:
    async def test_chain_defaults_to_empty_string(self, db: AsyncSession):
        """Existing (non-crypto) flows that omit chain must still work."""
        acc = await _make_account(db, "Stock-Acc", acct_type="brokerage", currency="USD")
        asset = await _make_asset(db, symbol="AAPL")
        h = AssetHolding(
            account_id=acc.id,
            asset_id=asset.id,
            quantity=Decimal("10"),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(h)
        await db.commit()
        assert h.chain == ""

    async def test_same_asset_different_chain_allowed(self, db: AsyncSession):
        """USDT on Ethereum and USDT on Arbitrum are distinct rows."""
        acc = await _make_account(db, "Wallet-USDT")
        asset = await _make_asset(db, symbol="USDT")
        db.add_all(
            [
                AssetHolding(
                    account_id=acc.id,
                    asset_id=asset.id,
                    chain="ethereum",
                    quantity=Decimal("100"),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                ),
                AssetHolding(
                    account_id=acc.id,
                    asset_id=asset.id,
                    chain="arbitrum",
                    quantity=Decimal("200"),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                ),
            ]
        )
        await db.commit()

    async def test_duplicate_account_asset_chain_blocked(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-DupHolding")
        asset = await _make_asset(db, symbol="ETH")
        db.add(
            AssetHolding(
                account_id=acc.id,
                asset_id=asset.id,
                chain="ethereum",
                quantity=Decimal("1"),
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await db.commit()

        async with _TestingSessionLocal() as session2:
            await session2.execute(text("PRAGMA foreign_keys=ON"))
            session2.add(
                AssetHolding(
                    account_id=acc.id,
                    asset_id=asset.id,
                    chain="ethereum",
                    quantity=Decimal("2"),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
            with pytest.raises(IntegrityError):
                await session2.commit()
