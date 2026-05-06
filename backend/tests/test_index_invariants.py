"""Tests for FIX-6: transaction table index invariants.

Verifies that:
1. All four expected indexes exist after lifespan runs.
2. The partial unique index on (account_id, external_id) enforces uniqueness
   for non-deleted rows with a non-NULL external_id.
"""

from __future__ import annotations

import os
import pytest
import pytest_asyncio
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Set test token before importing the app so Settings validation passes
_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base  # noqa: E402
from app.main import (  # noqa: E402
    _BALANCE_VIEW_DROP_SQL,
    _BALANCE_VIEW_SQL,
    _index_migrations,
)
from app.models import Account, Transaction  # noqa: E402

# ─── Test database setup ────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(
    _engine, expire_on_commit=False, class_=AsyncSession
)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    """Create tables, views, and indexes once for the entire module."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))
        # Apply index migrations (the same list used in lifespan)
        for _name, ddl in _index_migrations:
            await conn.execute(text(ddl))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _TestingSessionLocal() as session:
        yield session


# ─── Helpers ────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(db: AsyncSession, name: str = "Test") -> Account:
    acc = Account(
        name=name,
        type="bank",
        currency="CNY",
        initial_balance=Decimal("0"),
        is_active=True,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(acc)
    await db.flush()
    return acc


async def _make_tx(
    db: AsyncSession,
    account: Account,
    external_id: str | None = None,
    deleted_at: str | None = None,
) -> Transaction:
    tx = Transaction(
        account_id=account.id,
        occurred_at=_utcnow(),
        amount=Decimal("10"),
        currency="CNY",
        type="expense",
        source="manual",
        is_pending=False,
        external_id=external_id,
        deleted_at=deleted_at,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestIndexesExist:
    async def test_indexes_exist_after_lifespan(self, db: AsyncSession):
        """All four indexes created by _index_migrations must exist in sqlite_master."""
        expected_indexes = {
            "ix_transactions_account_id_occurred_at",
            "ix_transactions_category_id",
            "ix_transactions_pdf_import_id",
            "uq_transactions_external_id_per_account",
        }
        rows = (
            await db.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='transactions'"
                )
            )
        ).fetchall()
        actual_indexes = {row[0] for row in rows}
        for idx in expected_indexes:
            assert idx in actual_indexes, f"Expected index '{idx}' not found in sqlite_master"


class TestExternalIdUniqueIndex:
    async def test_duplicate_external_id_same_account_blocked(self, db: AsyncSession):
        """Two active rows with the same (account_id, external_id) must fail."""
        acc = await _make_account(db, "Acc-Dup")
        await _make_tx(db, acc, external_id="ext-001")
        await db.commit()

        async with _TestingSessionLocal() as session2:
            acc2 = await session2.get(Account, acc.id)
            with pytest.raises(IntegrityError):
                await _make_tx(session2, acc2, external_id="ext-001")
                await session2.commit()

    async def test_duplicate_external_id_different_accounts_allowed(self, db: AsyncSession):
        """Same external_id on different accounts must succeed."""
        acc_a = await _make_account(db, "Acc-A")
        acc_b = await _make_account(db, "Acc-B")
        await _make_tx(db, acc_a, external_id="shared-ext")
        await _make_tx(db, acc_b, external_id="shared-ext")
        await db.commit()  # must not raise

    async def test_soft_deleted_external_id_can_be_reused(self, db: AsyncSession):
        """After soft-deleting a row, the same (account_id, external_id) can be reused."""
        acc = await _make_account(db, "Acc-Soft")
        deleted_tx = await _make_tx(db, acc, external_id="reuse-ext", deleted_at=_utcnow())
        await db.commit()

        # Confirm the deleted row is there
        assert deleted_tx.deleted_at is not None

        # Insert a new active row with the same external_id — should succeed
        await _make_tx(db, acc, external_id="reuse-ext")
        await db.commit()  # must not raise

    async def test_null_external_id_does_not_collide(self, db: AsyncSession):
        """Two rows with external_id=None on the same account must both succeed."""
        acc = await _make_account(db, "Acc-Null")
        await _make_tx(db, acc, external_id=None)
        await _make_tx(db, acc, external_id=None)
        await db.commit()  # must not raise
