"""V6-P1-1: verify that pair_transactions and mark_subaccount_pair clear is_pending.

Bug: auto-paired PDF rows entered ingestion with is_pending=True but neither
pair_transactions nor mark_subaccount_pair ever cleared the flag, leaving
paired transfers stuck in the inbox UI and excluded from cashflow recompute.

Fix: both helpers now set is_pending=False on both legs (idempotent).
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
from app.main import (  # noqa: E402
    _BALANCE_VIEW_DROP_SQL,
    _BALANCE_VIEW_SQL,
    _index_migrations,
)
from app.models import Account, Transaction  # noqa: E402
from app.services.transfer_matcher.engine import (  # noqa: E402
    auto_pair_after_import,
    mark_subaccount_pair,
    pair_transactions,
)

# ─── Test DB ────────────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))
        for _name, ddl in _index_migrations:
            await conn.execute(text(ddl))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _Session() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


# ─── Helpers ────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(
    db: AsyncSession,
    name: str = "Test",
    account_type: str = "bank",
    iban: str | None = None,
) -> Account:
    acc = Account(
        name=name,
        type=account_type,
        currency="CNY",
        initial_balance=Decimal("0"),
        is_active=True,
        iban=iban,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(acc)
    await db.flush()
    return acc


async def _make_pending_tx(
    db: AsyncSession,
    account: Account,
    amount: Decimal,
    tx_type: str = "expense",
    description: str = "test tx",
    occurred_at: str | None = None,
) -> Transaction:
    tx = Transaction(
        account_id=account.id,
        occurred_at=occurred_at or _utcnow(),
        amount=amount,
        currency="CNY",
        type=tx_type,
        source="pdf_import",
        is_pending=True,  # PDF rows start pending
        description=description,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestPairTransactionsClearsPending:
    async def test_pair_transactions_clears_pending_on_both_legs(self, db: AsyncSession):
        """pair_transactions must set is_pending=False on both out and in legs."""
        acc_a = await _make_account(db, "BankA")
        acc_b = await _make_account(db, "BankB")

        out_tx = await _make_pending_tx(db, acc_a, Decimal("500.00"), tx_type="expense")
        in_tx = await _make_pending_tx(db, acc_b, Decimal("500.00"), tx_type="income")

        assert out_tx.is_pending is True
        assert in_tx.is_pending is True

        await pair_transactions(db, out_tx, in_tx)

        assert out_tx.is_pending is False, "out_tx.is_pending must be False after pair_transactions"
        assert in_tx.is_pending is False, "in_tx.is_pending must be False after pair_transactions"

    async def test_pair_transactions_also_sets_transfer_type(self, db: AsyncSession):
        """Sanity: pair_transactions still promotes both legs to type='transfer'."""
        acc_a = await _make_account(db, "BankC")
        acc_b = await _make_account(db, "BankD")

        out_tx = await _make_pending_tx(db, acc_a, Decimal("100.00"), tx_type="expense")
        in_tx = await _make_pending_tx(db, acc_b, Decimal("100.00"), tx_type="income")

        await pair_transactions(db, out_tx, in_tx)

        assert out_tx.type == "transfer"
        assert in_tx.type == "transfer"
        assert out_tx.counter_account_id == acc_b.id
        assert in_tx.counter_account_id == acc_a.id

    async def test_idempotent_when_already_false(self, db: AsyncSession):
        """pair_transactions must not error when is_pending is already False."""
        acc_a = await _make_account(db, "BankE")
        acc_b = await _make_account(db, "BankF")

        out_tx = await _make_pending_tx(db, acc_a, Decimal("200.00"), tx_type="expense")
        in_tx = await _make_pending_tx(db, acc_b, Decimal("200.00"), tx_type="income")

        # Pre-clear the flag
        out_tx.is_pending = False
        in_tx.is_pending = False

        # Must not raise
        await pair_transactions(db, out_tx, in_tx)

        assert out_tx.is_pending is False
        assert in_tx.is_pending is False


class TestMarkSubaccountPairClearsPending:
    async def test_mark_subaccount_pair_clears_pending(self, db: AsyncSession):
        """mark_subaccount_pair must set is_pending=False on both legs."""
        acc = await _make_account(db, "BankSub")

        out_tx = await _make_pending_tx(
            db, acc, Decimal("300.00"), tx_type="expense", description="To Saving Space"
        )
        in_tx = await _make_pending_tx(
            db, acc, Decimal("300.00"), tx_type="income", description="To Saving Space"
        )

        assert out_tx.is_pending is True
        assert in_tx.is_pending is True

        await mark_subaccount_pair(db, out_tx, in_tx)

        assert out_tx.is_pending is False, "out_tx.is_pending must be False after mark_subaccount_pair"
        assert in_tx.is_pending is False, "in_tx.is_pending must be False after mark_subaccount_pair"

    async def test_mark_subaccount_pair_sets_transfer_type(self, db: AsyncSession):
        """Sanity: mark_subaccount_pair promotes both to type='transfer'."""
        acc = await _make_account(db, "BankSub2")

        out_tx = await _make_pending_tx(
            db, acc, Decimal("50.00"), tx_type="expense", description="Space transfer"
        )
        in_tx = await _make_pending_tx(
            db, acc, Decimal("50.00"), tx_type="income", description="Space transfer"
        )

        await mark_subaccount_pair(db, out_tx, in_tx)

        assert out_tx.type == "transfer"
        assert in_tx.type == "transfer"

    async def test_mark_subaccount_idempotent_when_already_false(self, db: AsyncSession):
        """mark_subaccount_pair must not error when is_pending is already False."""
        acc = await _make_account(db, "BankSub3")

        out_tx = await _make_pending_tx(
            db, acc, Decimal("75.00"), tx_type="expense", description="Savings move"
        )
        in_tx = await _make_pending_tx(
            db, acc, Decimal("75.00"), tx_type="income", description="Savings move"
        )
        out_tx.is_pending = False
        in_tx.is_pending = False

        # Must not raise
        await mark_subaccount_pair(db, out_tx, in_tx)

        assert out_tx.is_pending is False
        assert in_tx.is_pending is False


class TestAutoPairAfterImportClearsPending:
    async def test_auto_pair_after_import_clears_pending_e2e(self, db: AsyncSession):
        """auto_pair_after_import must clear is_pending on cross-account matched rows.

        We create two pending rows (expense in acc_a, income in acc_b) with the
        same amount and the receiving account's IBAN in the expense description —
        enough for the matcher to score >= AUTO (75) and auto-pair them.
        """
        from datetime import datetime, timezone

        # Use a fixed date so the date-window query finds both rows
        fixed_date = "2024-01-15T10:00:00Z"

        acc_a = await _make_account(db, "AutoBankA", iban="DE89370400440532013000")
        acc_b = await _make_account(db, "AutoBankB", iban="GB29NWBK60161331926819")

        # out_tx: expense in acc_a, description contains acc_b's IBAN → score 90+
        out_tx = Transaction(
            account_id=acc_a.id,
            occurred_at=fixed_date,
            amount=Decimal("999.00"),
            currency="CNY",
            type="expense",
            source="pdf_import",
            is_pending=True,
            description=f"Transfer to {acc_b.iban}",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        # in_tx: income in acc_b
        in_tx = Transaction(
            account_id=acc_b.id,
            occurred_at=fixed_date,
            amount=Decimal("999.00"),
            currency="CNY",
            type="income",
            source="pdf_import",
            is_pending=True,
            description="Incoming transfer",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(out_tx)
        db.add(in_tx)
        await db.flush()

        result = await auto_pair_after_import(db, [out_tx.id, in_tx.id])

        # The pair should be auto-matched (score >= 75 from amount + IBAN)
        assert len(result["auto_paired"]) == 1, (
            f"Expected 1 auto-paired pair, got {result['auto_paired']}. "
            f"Suggested: {result['suggested']}"
        )

        # Both legs must now have is_pending=False
        assert out_tx.is_pending is False, "out_tx.is_pending must be False after auto_pair_after_import"
        assert in_tx.is_pending is False, "in_tx.is_pending must be False after auto_pair_after_import"
