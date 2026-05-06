"""Tests for FIX-13: multi-currency fold in the unified ingestion pipeline.

Verifies that ingest_transactions populates fx_rate_to_base + base_amount for
foreign-currency transactions and marks fx_missing when no rate path exists.

Review V2 §V2-P0-1 — closes V1 P1-2 partial.
"""

from __future__ import annotations

import json
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
from app.models import Account, FxRate, Transaction  # noqa: E402
from app.services.ingestion import ingest_transactions  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


def _make_account() -> Account:
    return Account(
        id=1,
        name="Checking",
        type="bank",
        currency="CNY",
        initial_balance=Decimal("0"),
        is_active=True,
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )


def _make_tx(currency: str, amount: Decimal, tx_type: str = "expense") -> Transaction:
    return Transaction(
        account_id=1,
        occurred_at="2026-05-01T00:00:00Z",
        amount=amount,
        currency=currency,
        type=tx_type,
        description="test tx",
        source="manual",
        is_pending=False,
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )


def _make_fx(base: str, quote: str, rate: Decimal, source: str = "test") -> FxRate:
    return FxRate(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        quoted_at="2026-05-01T00:00:00Z",
        source=source,
        created_at="2026-05-01T00:00:00Z",
    )


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        session.add(_make_account())
        await session.commit()
        yield session
    await engine.dispose()


# ─── 1. Same currency skips FX lookup ───────────────────────────────────────


@pytest.mark.asyncio
async def test_same_currency_skips_fx(db: AsyncSession) -> None:
    """CNY tx should not get fx_rate_to_base or base_amount set."""
    tx = _make_tx("CNY", Decimal("100"))
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()

    assert tx.fx_rate_to_base is None, "same-currency should not set fx_rate_to_base"
    assert tx.base_amount is None, "same-currency should not set base_amount"
    meta = json.loads(tx.metadata_json) if tx.metadata_json else {}
    assert not meta.get("fx_missing"), "fx_missing must not be set for same-currency"


# ─── 2. Foreign currency with direct rate folds correctly ───────────────────


@pytest.mark.asyncio
async def test_foreign_currency_direct_rate_folds(db: AsyncSession) -> None:
    """50 EUR expense with EUR→CNY rate=8.0 should set base_amount=400."""
    db.add(_make_fx("EUR", "CNY", Decimal("8.0")))
    await db.commit()

    tx = _make_tx("EUR", Decimal("50"))
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()

    assert tx.fx_rate_to_base == Decimal("8.0"), f"expected 8.0, got {tx.fx_rate_to_base}"
    assert tx.base_amount == Decimal("400"), f"expected 400, got {tx.base_amount}"


# ─── 3. Inverse rate works ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inverse_rate_resolves(db: AsyncSession) -> None:
    """100 USD income with only CNY→USD=0.14 stored → rate≈7.14, base≈714."""
    db.add(_make_fx("CNY", "USD", Decimal("0.14")))
    await db.commit()

    tx = _make_tx("USD", Decimal("100"), tx_type="income")
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()

    assert tx.fx_rate_to_base is not None, "fx_rate_to_base should be set via inverse"
    # 1/0.14 ≈ 7.142857...
    expected_rate = Decimal("1") / Decimal("0.14")
    assert abs(tx.fx_rate_to_base - expected_rate) < Decimal("0.0001"), (
        f"rate {tx.fx_rate_to_base} not close to {expected_rate}"
    )
    expected_amount = Decimal("100") * expected_rate
    assert abs(tx.base_amount - expected_amount) < Decimal("0.01"), (
        f"base_amount {tx.base_amount} not close to {expected_amount}"
    )


# ─── 4. No rate path → fx_missing flag, ingest still completes ──────────────


@pytest.mark.asyncio
async def test_no_rate_path_sets_fx_missing(db: AsyncSession) -> None:
    """No fx_rates seeded: GBP expense should mark fx_missing, not raise."""
    tx = _make_tx("GBP", Decimal("100"))
    db.add(tx)
    # Must not raise
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()

    assert tx.fx_rate_to_base is None, "no rate path → fx_rate_to_base stays NULL"
    assert tx.base_amount is None, "no rate path → base_amount stays NULL"
    meta = json.loads(tx.metadata_json or "{}")
    assert meta.get("fx_missing") is True, "fx_missing must be True"
    assert meta.get("fx_src") == "GBP"
    assert meta.get("fx_base") == "CNY"


# ─── 5. Caller-supplied base_amount/rate is not overwritten ─────────────────


@pytest.mark.asyncio
async def test_caller_supplied_base_amount_respected(db: AsyncSession) -> None:
    """Pre-set fx_rate_to_base + base_amount must survive ingestion unchanged."""
    # Seed a different EUR rate to prove we don't use it
    db.add(_make_fx("EUR", "CNY", Decimal("8.0")))
    await db.commit()

    tx = _make_tx("EUR", Decimal("50"))
    tx.base_amount = Decimal("999")
    tx.fx_rate_to_base = Decimal("8")
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()

    assert tx.base_amount == Decimal("999"), "caller base_amount must not be overwritten"
    assert tx.fx_rate_to_base == Decimal("8"), "caller fx_rate_to_base must not be overwritten"
