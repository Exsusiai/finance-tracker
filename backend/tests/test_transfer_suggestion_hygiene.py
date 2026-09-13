"""Transfer-suggestion hygiene (2026-09, from Aug UAT):

1. A row the user explicitly settled (categorization_method='user', non-
   transfer) must stop surfacing as a matcher candidate — an amount
   coincidence (€0.12 purchase vs €0.12 daily savings interest) kept
   resuggesting a confirmed expense forever.
2. A dismissed pair (metadata_json.dismissed_pair_tx_ids, written by the
   suggestions dismiss endpoint) must never be suggested again.
3. Score ≥ AUTO pairs stay visible in find_transfer_pairs output (the
   suggestions endpoint no longer filters them out — pairs that become
   eligible only after import, e.g. user marks both legs as transfer,
   were previously invisible AND never auto-applied).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

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
    SCORE_THRESHOLD_AUTO,
    find_transfer_pairs,
)

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


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(db: AsyncSession, name: str) -> Account:
    acc = Account(
        name=name, type="bank", currency="CNY", initial_balance=Decimal("0"),
        is_active=True, created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    await db.flush()
    return acc


async def _make_tx(
    db: AsyncSession,
    account: Account,
    amount: str,
    tx_type: str,
    description: str,
    *,
    categorization_method: str | None = None,
    metadata: dict | None = None,
) -> Transaction:
    tx = Transaction(
        account_id=account.id,
        occurred_at="2026-08-10T00:00:00Z",
        amount=Decimal(amount),
        currency="CNY",
        type=tx_type,
        source="pdf_import",
        is_pending=False,
        description=description,
        categorization_method=categorization_method,
        metadata_json=json.dumps(metadata) if metadata else None,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


class TestSuggestionHygiene:
    async def test_user_confirmed_expense_not_a_candidate(self, db: AsyncSession):
        acc_a = await _make_account(db, "CardA")
        acc_b = await _make_account(db, "BankB")
        confirmed = await _make_tx(
            db, acc_a, "0.12", "expense", "Google Cloud",
            categorization_method="user",
        )
        interest = await _make_tx(db, acc_b, "0.12", "income", "Savings interest")

        pairs = await find_transfer_pairs(db)
        involved = {confirmed.id, interest.id}
        hits = [c for c in pairs if {c.a.id, c.b.id} & involved]
        assert hits == [], f"user-confirmed expense still suggested: {hits}"

    async def test_rule_categorized_expense_still_a_candidate(self, db: AsyncSession):
        # Auto-categorization is a guess — those rows must KEEP pairing
        # (imported transfer legs routinely arrive as rule-categorized expense).
        acc_a = await _make_account(db, "BankA2")
        acc_b = await _make_account(db, "BankB2")
        out_tx = await _make_tx(
            db, acc_a, "77.70", "expense", "Transfer to BankB2",
            categorization_method="rule",
        )
        in_tx = await _make_tx(db, acc_b, "77.70", "income", "Payment from BankA2")

        pairs = await find_transfer_pairs(db)
        hits = [c for c in pairs if {c.a.id, c.b.id} == {out_tx.id, in_tx.id}]
        assert hits, "rule-categorized legs should still be pairable"

    async def test_dismissed_pair_never_resuggested(self, db: AsyncSession):
        acc_a = await _make_account(db, "BankA3")
        acc_b = await _make_account(db, "BankB3")
        out_tx = await _make_tx(db, acc_a, "33.33", "expense", "Transfer to BankB3")
        in_tx = await _make_tx(db, acc_b, "33.33", "income", "Payment from BankA3")

        pairs = await find_transfer_pairs(db)
        assert any({c.a.id, c.b.id} == {out_tx.id, in_tx.id} for c in pairs)

        # Dismiss (mirror of the endpoint's metadata stamp), then re-run.
        out_tx.metadata_json = json.dumps({"dismissed_pair_tx_ids": [in_tx.id]})
        in_tx.metadata_json = json.dumps({"dismissed_pair_tx_ids": [out_tx.id]})
        await db.flush()

        pairs = await find_transfer_pairs(db)
        hits = [c for c in pairs if {c.a.id, c.b.id} == {out_tx.id, in_tx.id}]
        assert hits == [], f"dismissed pair resurfaced: {hits}"

    async def test_auto_tier_pairs_remain_visible(self, db: AsyncSession):
        # Two unpaired transfer-typed legs with directions (post-import manual
        # marking) — high score, previously hidden by the endpoint's <AUTO
        # filter while nothing auto-applied them.
        acc_a = await _make_account(db, "PayPal")
        acc_b = await _make_account(db, "BankB4")
        out_tx = await _make_tx(
            db, acc_b, "23.50", "transfer", "PayPal Europe S.a.r.l.",
            metadata={"transfer_direction": "out"},
        )
        in_tx = await _make_tx(
            db, acc_a, "23.50", "transfer", "Bank Deposit to PP Account",
            metadata={"transfer_direction": "in"},
        )

        pairs = await find_transfer_pairs(db)
        hits = [c for c in pairs if {c.a.id, c.b.id} == {out_tx.id, in_tx.id}]
        assert hits, "unpaired transfer legs must appear as a candidate"
        assert hits[0].score >= SCORE_THRESHOLD_AUTO
