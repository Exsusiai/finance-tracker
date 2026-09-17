"""V9 fixes F07 / F08 / F14 / F16 — matcher evidence gating, direction
checks, base-currency lock, and user-decision protection.

Ported from `code review/repro_v9.py` after the fixes landed (assertions
unchanged — they describe correct behaviour).
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
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base  # noqa: E402
from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL, _index_migrations  # noqa: E402
from app.models import Account, Category, Transaction  # noqa: E402
from app.services.transfer_matcher.engine import (  # noqa: E402
    find_transfer_pairs,
    pair_transactions,
    replace_synthetic_with_real,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

DATE = "2026-09-10T12:00:00Z"


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


async def _account(db: AsyncSession, name: str) -> Account:
    a = Account(
        name=name, type="bank", currency="EUR", initial_balance=Decimal("0"),
        is_active=True, created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(a)
    await db.flush()
    return a


async def _tx(
    db: AsyncSession,
    account: Account,
    amount: str,
    tx_type: str,
    description: str,
    *,
    occurred_at: str = DATE,
    metadata: dict | None = None,
    categorization_method: str | None = None,
) -> Transaction:
    t = Transaction(
        account_id=account.id, occurred_at=occurred_at, amount=Decimal(amount),
        currency="EUR", type=tx_type, source="pdf_import", is_pending=False,
        description=description, categorization_method=categorization_method,
        metadata_json=json.dumps(metadata) if metadata else None,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(t)
    await db.flush()
    return t


async def _balance(db: AsyncSession, account_id: int) -> Decimal:
    v = (await db.execute(
        text("SELECT balance FROM v_account_balance WHERE account_id=:a"),
        {"a": account_id},
    )).scalar_one()
    return Decimal(str(v))


class TestF07AutoPairEvidence:
    async def test_amount_and_date_alone_do_not_auto_pair(self, db: AsyncSession):
        """Unrelated same-day same-amount rows across a first-ever account
        combo must only ever SUGGEST — not silently erase income/expense."""
        a = await _account(db, "AlphaF07")
        b = await _account(db, "BetaF07")
        await _tx(db, a, "100", "expense", "Groceries")
        await _tx(db, b, "100", "income", "Savings interest")

        pairs = await find_transfer_pairs(db)
        mine = [c for c in pairs if {c.a.account_id, c.b.account_id} == {a.id, b.id}]
        assert mine, "the coincidence should still surface as a suggestion"
        assert all(not c.auto for c in mine), "amount+date alone must not be auto"

    async def test_established_account_pair_still_autos(self, db: AsyncSession):
        """The compromise: accounts with a previously confirmed transfer
        between them keep auto-pairing on same-day equal amounts even when
        descriptions carry no account-name/IBAN evidence (real N26↔Revolut
        moves all look like 'Jingsheng Chen')."""
        a = await _account(db, "N26F07")
        b = await _account(db, "RevF07")
        prev_out = await _tx(db, a, "300", "transfer", "Jingsheng Chen",
                             occurred_at="2026-08-01T00:00:00Z",
                             metadata={"transfer_direction": "out"})
        prev_in = await _tx(db, b, "300", "transfer", "Payment from JC",
                            occurred_at="2026-08-01T00:00:00Z",
                            metadata={"transfer_direction": "in"})
        await pair_transactions(db, prev_out, prev_in)

        await _tx(db, a, "200", "expense", "Jingsheng Chen")
        await _tx(db, b, "200", "income", "Payment received")
        pairs = await find_transfer_pairs(db)
        new = [c for c in pairs
               if {c.a.account_id, c.b.account_id} == {a.id, b.id}
               and c.a.amount == Decimal("200")]
        assert new and new[0].auto, "established pair must keep auto-pairing"


class TestF08DirectionChecks:
    async def test_synthetic_replacement_requires_matching_direction(self, db: AsyncSession):
        a = await _account(db, "AlphaF08")
        b = await _account(db, "BetaF08")
        source = await _tx(db, a, "100", "expense", "outgoing leg")
        mirror = await _tx(db, b, "100", "income", "mirror leg")
        await pair_transactions(db, source, mirror)
        meta = json.loads(mirror.metadata_json)
        meta["synthetic_counterleg"] = True
        mirror.metadata_json = json.dumps(meta)
        real = await _tx(db, b, "100", "expense", "Groceries")
        await db.flush()

        before = await _balance(db, b.id)
        await replace_synthetic_with_real(db, real_tx=real)
        await db.flush()
        assert await _balance(db, b.id) == before, (
            "an unrelated -100 expense must not replace a +100 synthetic deposit"
        )


class TestF16UserDecisionProtection:
    async def test_redetect_respects_user_confirmation(self, db: AsyncSession):
        from app.services.refresh_matching import RefreshContext, step_redetect_type

        a = await _account(db, "AlphaF16")
        t = await _tx(db, a, "100", "expense", "SEPA direct debit Insurance",
                      categorization_method="user")
        await step_redetect_type(RefreshContext(db=db))
        assert t.type == "expense", "user-confirmed expense re-typed by automation"

    async def test_llm_result_discarded_after_manual_confirmation(self, db: AsyncSession, monkeypatch):
        from types import SimpleNamespace

        from app.services.llm import classifier
        from app.services.llm.provider import ClassificationResult

        async def runtime(_db):
            return SimpleNamespace(enabled=True, max_notes_in_prompt=0,
                                   use_grounding=False, confidence_threshold=0.8)

        async def budget(_db):
            return True, 0, 10

        monkeypatch.setattr(classifier.app_settings_svc, "get_llm_settings", runtime)
        monkeypatch.setattr(classifier, "check_budget", budget)

        a = await _account(db, "LlmF16")
        food = Category(name="FoodF16", kind="expense",
                        created_at=_utcnow(), updated_at=_utcnow())
        travel = Category(name="TravelF16", kind="expense",
                          created_at=_utcnow(), updated_at=_utcnow())
        db.add_all([food, travel])
        t = await _tx(db, a, "100", "expense", "some merchant")
        t.is_pending = True
        await db.commit()

        class Provider:
            model = "fake-test"

            async def classify(self, *args, **kwargs):
                # Manual confirmation lands while the LLM request is in flight.
                async with _Session() as user_session:
                    row = await user_session.get(Transaction, t.id)
                    row.category_id = travel.id
                    row.is_pending = False
                    row.categorization_method = "user"
                    await user_session.commit()
                return ClassificationResult(
                    category_path="FoodF16", confidence=0.99, reason="fake",
                    used_search=False, input_tokens=0, output_tokens=0, cost_usd=0,
                )

        outcome = await classifier.classify_with_llm(db, t, provider=Provider())
        await db.commit()
        assert outcome.matched is False

        async with _Session() as check:
            row = await check.get(Transaction, t.id)
            assert row.category_id == travel.id
            assert row.categorization_method == "user"
