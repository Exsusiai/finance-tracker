"""Tests for the unified ingestion pipeline (Sprint 1 FIX-4, review V1 §P1-3/5/7).

Covers the invariants the pipeline must enforce regardless of caller (PDF
upload, manual API, bank_sync, MCP):

1. Amount sign normalisation: non-`adjustment` rows always end up positive.
2. Categoriser hits auto-confirm pending rows.
3. Cashflow snapshot is rewritten for every period the batch touched
   (single-month + multi-month batches).
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
from app.models import Account, CategorizationRule, Category, Transaction  # noqa: E402
from app.services.ingestion import ingest_transactions  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        # Seed an account so foreign keys are happy
        session.add(
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
        await session.commit()
        yield session
    await engine.dispose()


# ─── 1. Amount sign normalisation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_negative_expense_amount_normalised_to_positive(db: AsyncSession) -> None:
    tx = Transaction(
        account_id=1,
        occurred_at="2026-05-01T00:00:00Z",
        amount=Decimal("-250.00"),  # bank-API style signed amount
        currency="CNY",
        type="expense",
        description="bank api row",
        source="bank_api",
        is_pending=False,
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()
    assert tx.amount == Decimal("250.00"), "expense should be stored as ABS"


@pytest.mark.asyncio
async def test_adjustment_amount_keeps_sign(db: AsyncSession) -> None:
    tx = Transaction(
        account_id=1,
        occurred_at="2026-05-01T00:00:00Z",
        amount=Decimal("-30.00"),  # legitimate adjustment can be negative
        currency="CNY",
        type="adjustment",
        description="manual balance correction",
        source="manual",
        is_pending=False,
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()
    assert tx.amount == Decimal("-30.00"), "adjustment must keep its signed delta"


# ─── 2. Categoriser hits auto-confirm pending rows ──────────────────────────


@pytest.mark.asyncio
async def test_rule_match_promotes_to_non_pending(db: AsyncSession) -> None:
    # Seed a category + rule
    cat = Category(
        id=100,
        name="Coffee",
        kind="expense",
        is_system=False,
        sort_order=0,
        created_at="2026-05-01T00:00:00Z",
    )
    db.add(cat)
    db.add(
        CategorizationRule(
            pattern="STARBUCKS",
            pattern_type="contains",
            field="description",
            category_id=100,
            priority=10,
            enabled=True,
            hit_count=0,
            created_at="2026-05-01T00:00:00Z",
        )
    )
    await db.flush()

    tx = Transaction(
        account_id=1,
        occurred_at="2026-05-02T00:00:00Z",
        amount=Decimal("4.50"),
        currency="CNY",
        type="expense",
        description="STARBUCKS Reserve Roastery",
        source="pdf_import",
        is_pending=True,  # ingestion should flip this
        created_at="2026-05-02T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )
    db.add(tx)
    result = await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()
    assert tx.is_pending is False
    assert tx.category_id == 100
    assert result.auto_categorized == 1


# ─── 3. Multi-month batch refreshes every snapshot ──────────────────────────


@pytest.mark.asyncio
async def test_multi_month_batch_recomputes_each_period(db: AsyncSession) -> None:
    txs = [
        Transaction(
            account_id=1,
            occurred_at="2026-04-15T00:00:00Z",
            amount=Decimal("100"),
            currency="CNY",
            type="income",
            description="april payday",
            source="manual",
            is_pending=False,
            created_at="2026-04-15T00:00:00Z",
            updated_at="2026-04-15T00:00:00Z",
        ),
        Transaction(
            account_id=1,
            occurred_at="2026-05-15T00:00:00Z",
            amount=Decimal("200"),
            currency="CNY",
            type="income",
            description="may payday",
            source="manual",
            is_pending=False,
            created_at="2026-05-15T00:00:00Z",
            updated_at="2026-05-15T00:00:00Z",
        ),
    ]
    for t in txs:
        db.add(t)
    result = await ingest_transactions(db, txs, auto_pair=False)
    await db.commit()
    assert (2026, 4) in result.affected_periods
    assert (2026, 5) in result.affected_periods

    rows = (await db.execute(
        text(
            "SELECT period_year, period_month, income_total "
            "FROM cash_flow_snapshots "
            "WHERE period_year=2026 AND period_month IN (4,5) "
            "ORDER BY period_month"
        )
    )).all()
    months = {(r[0], r[1]): float(r[2]) for r in rows}
    assert months.get((2026, 4)) == 100.0, f"april snapshot missing or wrong: {months}"
    assert months.get((2026, 5)) == 200.0, f"may snapshot missing or wrong: {months}"
