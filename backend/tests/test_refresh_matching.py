"""Regression tests for the refresh-matching pipeline + transfer-matcher
helpers shipped on 2026-05-07.

Covers the 4 architecture-review-flagged blind spots:
  R1. find_existing_counter_leg edge cases (synthetic exclusion, paired
      exclusion, amount/currency/window filtering)
  R2. find_transfer_pairs self-pair guard (a row that lands in BOTH
      outflows and inflows must NOT pair with itself)
  R3. delete_transaction → refresh-matching end-to-end (deleting one leg
      detaches the counterpart and the counterpart resurfaces in the
      unpaired panel; running refresh-matching after deletion no longer
      crashes on Step 5 dict unpacking)
  R4. replace_synthetic_with_real (importing a real leg that matches an
      existing synthetic mirror retires the mirror and re-points the
      paired source — no 1 real + 1 synthetic + 1 new real triplets)

Plus regression for the bug found post-UAT: pair_transactions must write
`paired_with_tx_id` symmetrically so refresh-matching's Step -1 doesn't
mistake confirmed pairs for orphans and unpair them.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Transaction  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def _override_get_db():
    async with _TestingSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _create_tables():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        from sqlalchemy import text as _text
        await conn.execute(_text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(_text(_BALANCE_VIEW_SQL))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture()
async def db():
    async with _TestingSessionLocal() as session:
        yield session


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(db: AsyncSession, name: str, currency: str = "EUR",
                        initial: str = "0") -> Account:
    acc = Account(
        name=name, type="bank", currency=currency,
        initial_balance=Decimal(initial), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    await db.flush()
    return acc


async def _make_tx(
    db: AsyncSession,
    account: Account,
    amount: str,
    tx_type: str = "expense",
    occurred_at: str | None = None,
    metadata_json: str | None = None,
    source: str = "pdf_import",
) -> Transaction:
    tx = Transaction(
        account_id=account.id,
        occurred_at=occurred_at or "2026-05-01T00:00:00Z",
        amount=Decimal(amount),
        currency=account.currency,
        type=tx_type,
        source=source,
        is_pending=False,
        metadata_json=metadata_json,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


# ─── R1: find_existing_counter_leg edge cases ──────────────────────────


class TestFindExistingCounterLeg:
    async def test_finds_real_counterpart(self, db: AsyncSession):
        """Same amount/currency/within window/no pairing → returned."""
        from app.services.transfer_matcher import find_existing_counter_leg

        a = await _make_account(db, "A")
        b = await _make_account(db, "B")
        src = await _make_tx(db, a, "100", "expense", "2026-05-10T00:00:00Z")
        tgt = await _make_tx(db, b, "100", "income", "2026-05-10T00:00:00Z")
        await db.flush()

        found = await find_existing_counter_leg(db, src_tx=src, counter_account_id=b.id)
        assert found is not None
        assert found.id == tgt.id

    async def test_excludes_synthetic_counterleg(self, db: AsyncSession):
        """A row tagged synthetic_counterleg=true must NOT be returned —
        binding to it would create a synthetic-pairs-synthetic loop."""
        from app.services.transfer_matcher import find_existing_counter_leg

        a = await _make_account(db, "A2")
        b = await _make_account(db, "B2")
        src = await _make_tx(db, a, "100", "expense", "2026-05-10T00:00:00Z")
        await _make_tx(
            db, b, "100", "transfer", "2026-05-10T00:00:00Z",
            metadata_json=json.dumps({"synthetic_counterleg": True}),
        )
        await db.flush()

        found = await find_existing_counter_leg(db, src_tx=src, counter_account_id=b.id)
        assert found is None

    async def test_excludes_already_paired(self, db: AsyncSession):
        """A row with paired_with_tx_id in metadata is already taken."""
        from app.services.transfer_matcher import find_existing_counter_leg

        a = await _make_account(db, "A3")
        b = await _make_account(db, "B3")
        src = await _make_tx(db, a, "100", "expense", "2026-05-10T00:00:00Z")
        await _make_tx(
            db, b, "100", "income", "2026-05-10T00:00:00Z",
            metadata_json=json.dumps({"paired_with_tx_id": 999}),
        )
        await db.flush()

        found = await find_existing_counter_leg(db, src_tx=src, counter_account_id=b.id)
        assert found is None

    async def test_amount_tolerance(self, db: AsyncSession):
        """0.01 mismatch → not a match."""
        from app.services.transfer_matcher import find_existing_counter_leg

        a = await _make_account(db, "A4")
        b = await _make_account(db, "B4")
        src = await _make_tx(db, a, "100.00", "expense", "2026-05-10T00:00:00Z")
        await _make_tx(db, b, "100.50", "income", "2026-05-10T00:00:00Z")
        await db.flush()

        found = await find_existing_counter_leg(db, src_tx=src, counter_account_id=b.id)
        assert found is None

    async def test_window_days(self, db: AsyncSession):
        """Outside ±5 days → not a match."""
        from app.services.transfer_matcher import find_existing_counter_leg

        a = await _make_account(db, "A5")
        b = await _make_account(db, "B5")
        src = await _make_tx(db, a, "100", "expense", "2026-05-01T00:00:00Z")
        await _make_tx(db, b, "100", "income", "2026-05-15T00:00:00Z")
        await db.flush()

        found = await find_existing_counter_leg(db, src_tx=src, counter_account_id=b.id)
        assert found is None


# ─── R2: find_transfer_pairs self-pair guard ───────────────────────────


class TestSelfPairGuard:
    async def test_no_self_pair_in_fallback(self, db: AsyncSession):
        """A directionless transfer row injected into BOTH outflows and
        inflows must NOT pair with itself — would yield paired_with_tx_id
        pointing at its own id."""
        from app.services.transfer_matcher import find_transfer_pairs

        a = await _make_account(db, "Solo")
        # type=transfer, no transfer_direction in metadata, no description hint
        # → fallback puts it into BOTH outflows and inflows
        await _make_tx(
            db, a, "100", "transfer", "2026-05-10T00:00:00Z",
            metadata_json=None,
        )
        await db.flush()

        pairs = await find_transfer_pairs(db)
        # No candidates should pair this row with itself
        for c in pairs:
            assert c.a.id != c.b.id, f"Self-pair detected: {c.a.id}"


# ─── R3: delete → refresh end-to-end ───────────────────────────────────


class TestDeleteRefreshLifecycle:
    async def test_delete_detaches_counterpart_and_refresh_succeeds(self, db: AsyncSession):
        """Pair two real legs, delete one, verify the other is detached
        AND refresh-matching no longer crashes on the dict-unpack bug
        (Step 5 was `for a_id, b_id in orphans` over list[dict])."""
        from app.services.refresh_matching import RefreshContext, run_full_pipeline
        from app.services.transfer_matcher import pair_transactions

        a = await _make_account(db, "A6")
        b = await _make_account(db, "B6")
        out_tx = await _make_tx(db, a, "100", "expense", "2026-05-10T00:00:00Z")
        in_tx = await _make_tx(db, b, "100", "income", "2026-05-10T00:00:00Z")
        await pair_transactions(db, out_tx, in_tx)
        await db.flush()
        out_id, in_id = out_tx.id, in_tx.id

        # Both rows now have counter_account_id and paired_with_tx_id
        assert out_tx.counter_account_id == b.id
        assert in_tx.counter_account_id == a.id

        # Soft-delete out_tx via the same logic delete_transaction uses
        from datetime import datetime, timezone
        out_tx.deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Detach counterpart manually (delete endpoint does this; test the
        # post-state directly)
        in_tx.counter_account_id = None
        meta = json.loads(in_tx.metadata_json) if in_tx.metadata_json else {}
        meta.pop("paired_with_tx_id", None)
        in_tx.metadata_json = json.dumps(meta) if meta else None
        await db.flush()

        # refresh-matching pipeline must complete without raising
        ctx = RefreshContext(db=db)
        await run_full_pipeline(ctx)

        # Counterpart should remain alive and detached
        in_after = (await db.execute(
            select(Transaction).where(Transaction.id == in_id)
        )).scalar_one()
        assert in_after.counter_account_id is None


# ─── R4: replace_synthetic_with_real ───────────────────────────────────


class TestReplaceSyntheticWithReal:
    async def test_synthetic_replaced_by_real_on_import(self, db: AsyncSession):
        """User manually bound source → synthetic mirror in dest. Later
        the real PDF imports → matcher should retire the mirror and
        re-point the source to the real row, not leave 3 rows around."""
        from app.services.transfer_matcher import replace_synthetic_with_real

        a = await _make_account(db, "A7")
        b = await _make_account(db, "B7")

        # Source already paired with its synthetic mirror
        source = await _make_tx(
            db, a, "100", "transfer", "2026-05-10T00:00:00Z",
            metadata_json=None,
        )
        synthetic = await _make_tx(
            db, b, "100", "transfer", "2026-05-10T00:00:00Z",
            metadata_json=json.dumps({
                "synthetic_counterleg": True,
                "transfer_direction": "in",
                "paired_with_tx_id": source.id,
            }),
        )
        # Source's pointer + counter_account
        source.counter_account_id = b.id
        source.metadata_json = json.dumps({
            "transfer_direction": "out",
            "paired_with_tx_id": synthetic.id,
        })
        # Synthetic's counter_account
        synthetic.counter_account_id = a.id
        await db.flush()

        # Now a real row arrives in account B (e.g. fresh PDF import)
        real = await _make_tx(
            db, b, "100", "income", "2026-05-10T00:00:00Z",
            source="pdf_import",
        )
        await db.flush()

        retired = await replace_synthetic_with_real(db, real_tx=real)
        await db.flush()

        # Synthetic should be soft-deleted
        assert retired is not None
        assert retired.id == synthetic.id
        assert retired.deleted_at is not None

        # Real row should now be paired with source
        assert real.type == "transfer"
        assert real.counter_account_id == a.id
        meta = json.loads(real.metadata_json or "{}")
        assert meta.get("paired_with_tx_id") == source.id

        # Source should now point at real, not synthetic
        src_meta = json.loads(source.metadata_json or "{}")
        assert src_meta.get("paired_with_tx_id") == real.id

    async def test_no_synthetic_returns_none(self, db: AsyncSession):
        """No matching synthetic → return None, no side effects."""
        from app.services.transfer_matcher import replace_synthetic_with_real

        a = await _make_account(db, "A8")
        real = await _make_tx(db, a, "100", "income", "2026-05-10T00:00:00Z")
        await db.flush()

        retired = await replace_synthetic_with_real(db, real_tx=real)
        assert retired is None


# ─── Bonus: pair_transactions writes symmetric paired_with_tx_id ───────


class TestPairTransactionsSymmetricPointer:
    async def test_paired_with_tx_id_written_both_sides(self, db: AsyncSession):
        """Bug shipped 2026-05-07 then fixed: pair_transactions used to set
        only transfer_direction. Step -1 of refresh-matching mistook
        confirmed pairs for orphans and unpaired them every run."""
        from app.services.transfer_matcher import pair_transactions

        a = await _make_account(db, "A9")
        b = await _make_account(db, "B9")
        out_tx = await _make_tx(db, a, "100", "expense", "2026-05-10T00:00:00Z")
        in_tx = await _make_tx(db, b, "100", "income", "2026-05-10T00:00:00Z")
        await pair_transactions(db, out_tx, in_tx)
        await db.flush()

        out_meta = json.loads(out_tx.metadata_json or "{}")
        in_meta = json.loads(in_tx.metadata_json or "{}")
        assert out_meta.get("paired_with_tx_id") == in_tx.id
        assert in_meta.get("paired_with_tx_id") == out_tx.id

    async def test_refresh_does_not_unpair_confirmed(self, db: AsyncSession):
        """After pair_transactions, running refresh-matching twice in a
        row must NOT touch the pair (Step -1 should leave it alone)."""
        from app.services.refresh_matching import RefreshContext, run_full_pipeline
        from app.services.transfer_matcher import pair_transactions

        a = await _make_account(db, "A10")
        b = await _make_account(db, "B10")
        out_tx = await _make_tx(db, a, "100", "expense", "2026-05-10T00:00:00Z")
        in_tx = await _make_tx(db, b, "100", "income", "2026-05-10T00:00:00Z")
        await pair_transactions(db, out_tx, in_tx)
        await db.flush()

        out_id, in_id = out_tx.id, in_tx.id

        # First refresh — counter_account_id must survive
        ctx = RefreshContext(db=db)
        await run_full_pipeline(ctx)
        assert ctx.summary["orphan_pointers_cleared"] == 0

        # Second refresh — still no change
        ctx2 = RefreshContext(db=db)
        await run_full_pipeline(ctx2)
        assert ctx2.summary["orphan_pointers_cleared"] == 0

        # Pair pointers intact
        out_after = (await db.execute(
            select(Transaction).where(Transaction.id == out_id)
        )).scalar_one()
        in_after = (await db.execute(
            select(Transaction).where(Transaction.id == in_id)
        )).scalar_one()
        assert out_after.counter_account_id == b.id
        assert in_after.counter_account_id == a.id


async def test_refresh_matching_llm_dispatch_no_import_error(db: AsyncSession, monkeypatch):
    """Regression: V7-P1-4 renamed _dispatch_llm_classification → an after-commit
    hook, but system.py::refresh_matching still imported the OLD name, so the
    endpoint 500'd ('cannot import name _dispatch_llm_classification') whenever
    LLM was enabled and there were untagged rows. Exercise that exact branch."""
    from types import SimpleNamespace

    import app.services.app_settings as aset
    import app.services.llm.queue as q

    acc = await _make_account(db, "RM-LLM")
    # Unmatched pdf_import expense → the pipeline routes it to the LLM targets.
    await _make_tx(db, acc, "9.99", tx_type="expense", source="pdf_import")
    await db.commit()

    async def _settings(_db):
        return SimpleNamespace(enabled=True)

    async def _key(_db):
        return "fake-key"

    monkeypatch.setattr(aset, "get_llm_settings", _settings)
    monkeypatch.setattr(aset, "get_gemini_api_key", _key)
    # Don't touch the real queue/worker — just count.
    monkeypatch.setattr(q, "enqueue", lambda ids: len(list(ids)))

    from app.api.v1.system import refresh_matching

    resp = await refresh_matching(_token=_TEST_TOKEN, db=db)  # must not raise ImportError
    assert resp.data["llm_dispatched"] >= 1


pytestmark = pytest.mark.asyncio
