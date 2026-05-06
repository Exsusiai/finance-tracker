"""Tests for FIX-14 (category kind guard) and FIX-16 (safe regex) in the
categorization engine and rules API.

FIX-14 scenarios:
- rule_kind_mismatch_does_not_categorize: expense-kind rule against an income tx → skipped.
- rule_kind_match_categorizes: same rule against an expense tx → matched.
- apply_all_rules_respects_kind: POST /rules/apply-all only categorizes tx whose type
  matches the rule's target category kind.

FIX-16 scenarios:
- rules_test_safe_regex_matches: POST /rules/test with a normal regex → matched=true.
- rules_test_unsafe_regex_rejected: POST /rules/test against a rule whose pattern has
  nested quantifiers is pre-blocked at rule creation (422 on POST /rules).
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "cafebabecafebabecafebabecafebabecafebabe"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, CategorizationRule, Transaction  # noqa: E402
from app.services.categorizer.engine import categorize_transaction  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def override_get_db():
    async with _TestingSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}", "Content-Type": "application/json"}

# Fixed IDs to avoid collisions with other test modules.
_ACCT_ID = 2001
_CAT_EXPENSE_ID = 3001  # kind=expense
_CAT_INCOME_ID = 3002   # kind=income
_RULE_ID_BASE = 4001    # expense-kind rule matching "STARBUCKS"


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_schema():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _TestingSessionLocal() as s:
        s.add(Account(
            id=_ACCT_ID,
            name="Safety Test Account",
            type="bank",
            currency="CNY",
            initial_balance=Decimal("0"),
            is_active=True,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        ))
        s.add(Category(
            id=_CAT_EXPENSE_ID,
            name="Safety Expense Cat",
            kind="expense",
            is_system=False,
            sort_order=0,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        ))
        s.add(Category(
            id=_CAT_INCOME_ID,
            name="Safety Income Cat",
            kind="income",
            is_system=False,
            sort_order=0,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        ))
        # Expense-kind rule that matches "STARBUCKS"
        s.add(CategorizationRule(
            id=_RULE_ID_BASE,
            pattern="STARBUCKS",
            pattern_type="contains",
            field="description",
            category_id=_CAT_EXPENSE_ID,
            priority=10,
            enabled=True,
            hit_count=0,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        ))
        await s.commit()

    yield

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── FIX-14: kind guard in categorize_transaction ───────────────────────────

@pytest.mark.asyncio
async def test_rule_kind_mismatch_does_not_categorize() -> None:
    """expense-kind rule must not attach to an income transaction."""
    async with _TestingSessionLocal() as db:
        tx = Transaction(
            account_id=_ACCT_ID,
            occurred_at="2026-05-10T00:00:00Z",
            amount=Decimal("100.00"),
            currency="CNY",
            type="income",  # rule targets expense
            description="STARBUCKS bonus",
            source="pdf_import",
            created_at="2026-05-10T00:00:00Z",
            updated_at="2026-05-10T00:00:00Z",
        )
        db.add(tx)
        await db.flush()

        matched = await categorize_transaction(db, tx)

    assert not matched, "income tx must not match an expense-kind rule"
    assert tx.category_id is None


@pytest.mark.asyncio
async def test_rule_kind_match_categorizes() -> None:
    """expense-kind rule must attach when the tx is also expense."""
    async with _TestingSessionLocal() as db:
        tx = Transaction(
            account_id=_ACCT_ID,
            occurred_at="2026-05-11T00:00:00Z",
            amount=Decimal("5.50"),
            currency="CNY",
            type="expense",
            description="STARBUCKS coffee",
            source="pdf_import",
            created_at="2026-05-11T00:00:00Z",
            updated_at="2026-05-11T00:00:00Z",
        )
        db.add(tx)
        await db.flush()

        matched = await categorize_transaction(db, tx)

    assert matched, "expense tx must match the expense-kind rule"
    assert tx.category_id == _CAT_EXPENSE_ID


# ─── FIX-14: kind guard in /rules/apply-all ─────────────────────────────────

@pytest.mark.asyncio
async def test_apply_all_rules_respects_kind(client: AsyncClient) -> None:
    """POST /rules/apply-all should only categorize tx whose type matches the rule's
    target category kind."""
    # Insert one expense tx and one income tx, both matching the rule's pattern.
    async with _TestingSessionLocal() as s:
        expense_tx = Transaction(
            account_id=_ACCT_ID,
            occurred_at="2026-05-12T00:00:00Z",
            amount=Decimal("4.00"),
            currency="CNY",
            type="expense",
            description="STARBUCKS latte",
            source="pdf_import",
            created_at="2026-05-12T00:00:00Z",
            updated_at="2026-05-12T00:00:00Z",
        )
        income_tx = Transaction(
            account_id=_ACCT_ID,
            occurred_at="2026-05-12T01:00:00Z",
            amount=Decimal("999.00"),
            currency="CNY",
            type="income",
            description="STARBUCKS refund",
            source="pdf_import",
            created_at="2026-05-12T01:00:00Z",
            updated_at="2026-05-12T01:00:00Z",
        )
        s.add(expense_tx)
        s.add(income_tx)
        await s.commit()
        expense_id = expense_tx.id
        income_id = income_tx.id

    r = await client.post("/api/v1/rules/apply-all", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # At least the expense tx should have been matched.
    assert data["matched"] >= 1

    # Verify DB state: expense categorized, income not.
    from sqlalchemy import select as sa_select
    async with _TestingSessionLocal() as s:
        e = (await s.execute(sa_select(Transaction).where(Transaction.id == expense_id))).scalar_one()
        i = (await s.execute(sa_select(Transaction).where(Transaction.id == income_id))).scalar_one()

    assert e.category_id == _CAT_EXPENSE_ID, "expense tx should be categorized"
    assert i.category_id is None, "income tx must not be categorized by an expense-kind rule"


# ─── FIX-16: safe regex via /rules/test ─────────────────────────────────────

@pytest.mark.asyncio
async def test_rules_test_safe_regex_matches(client: AsyncClient) -> None:
    """POST /rules/test with a safe regex against a matching description returns matched=true."""
    # Create a regex rule first
    create_r = await client.post(
        "/api/v1/rules",
        json={
            "pattern": "STARBUCKS|MCDONALDS",
            "pattern_type": "regex",
            "field": "description",
            "category_id": _CAT_EXPENSE_ID,
            "priority": 1,
            "enabled": True,
        },
        headers=AUTH,
    )
    assert create_r.status_code == 201, create_r.text

    r = await client.post(
        "/api/v1/rules/test",
        json={"description": "MCDONALDS big mac"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["matched"] is True


@pytest.mark.asyncio
async def test_rules_create_with_unsafe_regex_rejected(client: AsyncClient) -> None:
    """POST /rules with a catastrophically-backtracking regex → 422."""
    r = await client.post(
        "/api/v1/rules",
        json={
            "pattern": "(a+)+b",
            "pattern_type": "regex",
            "field": "description",
            "category_id": _CAT_EXPENSE_ID,
            "priority": 0,
            "enabled": True,
        },
        headers=AUTH,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_create_rule_with_nonexistent_category_rejected(client: AsyncClient) -> None:
    """POST /rules with a non-existent category_id → 404."""
    r = await client.post(
        "/api/v1/rules",
        json={
            "pattern": "AMAZON",
            "pattern_type": "contains",
            "field": "description",
            "category_id": 99999,
            "priority": 0,
            "enabled": True,
        },
        headers=AUTH,
    )
    assert r.status_code == 404, r.text
