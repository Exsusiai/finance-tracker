"""Regression tests for LLM dispatch correctness.

V6-P1-6 (original): asyncio.create_task fired the classifier before the parent
session committed, so the worker's independent session saw no row.

V7-P1-4 (current): the old fix — ``await db.commit()`` mid-pipeline — broke the
caller's transaction boundary (a later failure could no longer roll back the
inserted rows). The new contract: ``ingest_transactions`` does NOT commit; it
registers a one-shot ``after_commit`` hook that enqueues the rows. So:

- enqueue happens only AFTER the caller's commit (rows are durable for the
  worker's independent session), and
- a rollback enqueues NOTHING (no orphan classification of rows that vanished).

Coverage
--------
test_enqueue_fires_after_commit
    enqueue is NOT called by ingest_transactions itself; it fires on the
    caller's commit, and by then the row is findable in a fresh session.
test_rollback_enqueues_nothing
    if the caller rolls back instead of committing, enqueue never fires.
test_worker_uses_independent_session
    _classify_one_with_llm opens its OWN session, leaving the caller's intact.

All tests are deterministic — no asyncio.sleep timing tricks for the enqueue
assertions.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "aabbccddeeff00112233445566778899aabbccdd"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    CategorizationRule,
    Category,
    Transaction,
    _utcnow_str,
)
from app.services.app_settings import seed_defaults  # noqa: E402
from app.services.ingestion import ingest_transactions  # noqa: E402
from app.services.llm.provider import ClassificationResult  # noqa: E402


# ─── Shared in-memory database ───────────────────────────────────────────────

_TEST_DB_URL = "sqlite+aiosqlite:///file:llm_dispatch_race?mode=memory&cache=shared&uri=true"
_engine = create_async_engine(_TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="function")
async def db():
    """Fresh schema + seeded account + LLM defaults for each test."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with _Session() as session:
        session.add(Account(
            id=1, name="Test Bank", type="bank", currency="EUR",
            initial_balance=Decimal("0"), is_active=True,
            created_at=_utcnow_str(), updated_at=_utcnow_str(),
        ))
        session.add(Category(
            id=10, name="餐饮", kind="expense", is_system=False, sort_order=0,
            created_at=_utcnow_str(), updated_at=_utcnow_str(),
        ))
        session.add(Category(
            id=11, name="快餐", kind="expense", parent_id=10,
            is_system=False, sort_order=0,
            created_at=_utcnow_str(), updated_at=_utcnow_str(),
        ))
        # A "polluted" rule so the tx is routed to LLM (requires_llm=True)
        session.add(CategorizationRule(
            id=1, pattern="Coffee", pattern_type="contains",
            field="description", category_id=11, priority=5,
            enabled=True, hit_count=0, requires_llm=True,
            created_at=_utcnow_str(), updated_at=_utcnow_str(),
        ))
        await seed_defaults(session)
        await session.commit()

    async with _Session() as session:
        yield session


def _coffee_tx() -> Transaction:
    return Transaction(
        account_id=1,
        occurred_at="2026-05-01T00:00:00Z",
        amount=Decimal("5.50"),
        currency="EUR",
        type="expense",
        description="Coffee shop",
        source="pdf_import",
        is_pending=True,
        created_at=_utcnow_str(),
        updated_at=_utcnow_str(),
    )


# ─── test_enqueue_fires_after_commit ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_fires_after_commit(db: AsyncSession, monkeypatch):
    """enqueue must fire on the caller's commit, not inside ingest_transactions.

    Pre-V7-P1-4 ingestion committed mid-pipeline. Now it stays atomic: the
    after_commit hook enqueues only once the caller commits, and by then the
    row is durable for the worker's independent session.
    """
    captured: list[int] = []
    # Auto-dispatch is opt-in (llm_auto_classify); enable it so these tests
    # exercise the after-commit enqueue mechanics they were written for.
    from app.services import app_settings as _aps
    await _aps.set_setting(db, "llm_auto_classify", True)
    monkeypatch.setattr(
        "app.services.llm.queue.enqueue",
        lambda ids: (captured.extend(ids), len(ids))[1],
    )

    tx = _coffee_tx()
    db.add(tx)
    result = await ingest_transactions(db, [tx], auto_pair=False)

    # Routed to LLM, but NOT yet enqueued — ingestion no longer commits.
    assert result.llm_dispatched == 1
    assert captured == [], "enqueue fired before commit — atomicity broken"

    await db.commit()
    assert len(captured) == 1, "enqueue did not fire on commit"
    tx_id = captured[0]

    # The worker (independent session) must now find the committed row.
    async with _Session() as independent_session:
        found = await independent_session.get(Transaction, tx_id)
        assert found is not None and found.description == "Coffee shop"


# ─── test_no_auto_dispatch_by_default ────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_auto_dispatch_by_default(db: AsyncSession, monkeypatch):
    """Default (llm_auto_classify off): an unmatched import row is NOT enqueued;
    it waits in the inbox for the manual 'AI 智能处理' trigger. Only L1 ran."""
    captured: list[int] = []
    monkeypatch.setattr(
        "app.services.llm.queue.enqueue",
        lambda ids: (captured.extend(ids), len(ids))[1],
    )

    tx = _coffee_tx()
    db.add(tx)
    result = await ingest_transactions(db, [tx], auto_pair=False)
    await db.commit()

    assert result.llm_dispatched == 0, "L2 must not auto-dispatch when off"
    assert captured == [], "row was enqueued despite auto-classify being off"
    assert tx.is_pending is True, "unmatched row should stay in the inbox"


# ─── test_rollback_enqueues_nothing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_enqueues_nothing(db: AsyncSession, monkeypatch):
    """A rollback after ingestion must enqueue nothing (no orphan classify)."""
    captured: list[int] = []
    # Auto-dispatch is opt-in (llm_auto_classify); enable it so these tests
    # exercise the after-commit enqueue mechanics they were written for.
    from app.services import app_settings as _aps
    await _aps.set_setting(db, "llm_auto_classify", True)
    monkeypatch.setattr(
        "app.services.llm.queue.enqueue",
        lambda ids: (captured.extend(ids), len(ids))[1],
    )

    tx = _coffee_tx()
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)

    await db.rollback()
    assert captured == [], "enqueue fired despite rollback — would classify a vanished row"


@pytest.mark.asyncio
async def test_rollback_then_commit_enqueues_nothing(db: AsyncSession, monkeypatch):
    """V8-P2-1: if the ingestion transaction rolls back and the SAME session is
    later reused to commit other work, the rolled-back tx ids must NOT be
    enqueued (the after_rollback hook cancels them)."""
    captured: list[int] = []
    # Auto-dispatch is opt-in (llm_auto_classify); enable it so these tests
    # exercise the after-commit enqueue mechanics they were written for.
    from app.services import app_settings as _aps
    await _aps.set_setting(db, "llm_auto_classify", True)
    monkeypatch.setattr(
        "app.services.llm.queue.enqueue",
        lambda ids: (captured.extend(ids), len(ids))[1],
    )

    tx = _coffee_tx()
    db.add(tx)
    await ingest_transactions(db, [tx], auto_pair=False)
    await db.rollback()

    # Reuse the session for an unrelated committed write.
    from app.models import Category, _utcnow_str
    db.add(Category(name="ZZTop", kind="expense", is_system=False, sort_order=99,
                    created_at=_utcnow_str(), updated_at=_utcnow_str()))
    await db.commit()

    assert captured == [], "rolled-back tx ids enqueued on a later commit"


# ─── test_worker_uses_independent_session ────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_uses_independent_session(db: AsyncSession, monkeypatch):
    """_classify_one_with_llm opens its own session, never reusing the caller's.

    Reusing the request session would cause 'Session is closed' errors once the
    request finishes. We inject a fake classifier that records its session id,
    then run the REAL _classify_one_with_llm and confirm it used a fresh session
    and left the caller's session usable.
    """
    tx = _coffee_tx()
    db.add(tx)
    await db.flush()
    tx_id = tx.id
    assert tx_id is not None
    await db.commit()

    worker_session_ids: list[int] = []

    async def _fake_classify(session, found, provider=None):  # noqa: ANN001
        worker_session_ids.append(id(session))
        return ClassificationResult(
            category_path="餐饮/快餐", confidence=0.9, reason="stub",
            used_search=False, input_tokens=1, output_tokens=1, cost_usd=0.0,
        )

    # _classify_one_with_llm imports both lazily, so patching the source
    # attributes takes effect at call time. Point the worker's session factory
    # at the test DB so the real function can find the committed row.
    monkeypatch.setattr("app.services.llm.classifier.classify_with_llm", _fake_classify)
    monkeypatch.setattr("app.db.async_session_factory", _Session)

    from app.services.ingestion import _classify_one_with_llm
    await _classify_one_with_llm(tx_id)

    assert len(worker_session_ids) == 1, "worker should open exactly one session"
    assert worker_session_ids[0] != id(db), (
        "worker must not reuse the caller's session"
    )

    # The original session must still be usable after the worker ran.
    from sqlalchemy import select
    row = (await db.execute(select(Transaction).where(Transaction.id == tx_id))).scalar_one_or_none()
    assert row is not None
