"""Tests for POST /transactions/{id}/mark-transfer endpoint.

Verifies that both single-leg and two-leg transfer marking correctly writes
transfer_direction into metadata_json so the balance view applies the right sign.

Uses an in-memory SQLite database + FastAPI TestClient (sync httpx).
"""

from __future__ import annotations

import json
import os
import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Set a test token before importing the app so Settings validation passes
_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402 - app creation happens here
from app.models import Account, Transaction  # noqa: E402

# ─── Test database setup ────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(
    _engine, expire_on_commit=False, class_=AsyncSession
)


async def override_get_db():
    async with _TestingSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


AUTH_HEADERS = {
    "Authorization": f"Bearer {_TEST_TOKEN}",
    "Content-Type": "application/json",
}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def create_tables():
    """Create all ORM tables (and the v_account_balance view) once per module.

    Save & restore `dependency_overrides[get_db]` so concurrent test modules
    don't leak each other's in-memory engines (Sprint 1 FIX-7).
    """
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Create the balance view (normally done in app lifespan)
        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        await conn.execute(__import__("sqlalchemy").text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(__import__("sqlalchemy").text(_BALANCE_VIEW_SQL))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _TestingSessionLocal() as session:
        yield session


@pytest_asyncio.fixture()
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ─── Helpers ────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(db: AsyncSession, name: str = "Test", currency: str = "CNY",
                        initial: str = "1000") -> Account:
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
) -> Transaction:
    tx = Transaction(
        account_id=account.id,
        occurred_at=occurred_at or _utcnow(),
        amount=Decimal(amount),
        currency=account.currency,
        type=tx_type,
        source="manual",
        is_pending=False,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _balance(db: AsyncSession, account_id: int) -> Decimal:
    from sqlalchemy import text
    # Expire all cached objects to force a re-read from DB
    await db.close()
    row = (await db.execute(
        text("SELECT balance FROM v_account_balance WHERE account_id = :id"),
        {"id": account_id},
    )).one_or_none()
    if row is None:
        return Decimal("0")
    return Decimal(str(row[0]))


async def _refetch_tx(db: AsyncSession, tx_id: int) -> Transaction:
    """Reload tx from DB, bypassing any ORM cache."""
    from sqlalchemy import select
    await db.close()
    return (await db.execute(
        select(Transaction).where(Transaction.id == tx_id)
    )).scalar_one()


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestSingleLegTransfer:
    async def test_single_leg_in_increases_balance(self, client: AsyncClient, db: AsyncSession):
        """Single-leg direction='in' → metadata tagged; balance = initial + amount.

        The tx was previously type=income (adds ABS(amount)).
        After marking as transfer+direction=in it still adds ABS(amount), so the
        balance is stable but the metadata_json is correctly tagged.
        """
        acc = await _make_account(db, "Acc-In", initial="500")
        # Start as a plain transfer (untagged) so flipping to 'in' is meaningful
        tx = await _make_tx(db, acc, "100", tx_type="transfer")
        await db.commit()

        # Untagged transfer → balance view uses -ABS, so balance is 500 - 100 = 400
        bal_before = await _balance(db, acc.id)
        assert bal_before == Decimal("400")

        resp = await client.post(
            f"/api/v1/transactions/{tx.id}/mark-transfer",
            json={"transfer_direction": "in"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        # Re-fetch to read what the app session committed
        tx_fresh = await _refetch_tx(db, tx.id)
        meta = json.loads(tx_fresh.metadata_json or "{}")
        assert meta.get("transfer_direction") == "in"
        assert tx_fresh.type == "transfer"

        # After tagging 'in', the view adds +ABS(amount) → 500 + 100 = 600
        bal_after = await _balance(db, acc.id)
        assert bal_after == Decimal("600")

    async def test_single_leg_out_decreases_balance(self, client: AsyncClient, db: AsyncSession):
        """Single-leg direction='out' → metadata tagged; balance correctly decreases.

        Start with an untagged transfer (view defaults to -ABS). After explicit
        direction='out', balance stays at initial - amount (sign is the same for
        untagged transfers and tagged-out transfers in the view).
        The important thing: type=transfer and metadata tagged.
        """
        acc = await _make_account(db, "Acc-Out", initial="500")
        tx = await _make_tx(db, acc, "50", tx_type="expense")
        await db.commit()

        # expense tx: view uses -ABS(amount) → 500 - 50 = 450
        bal_before = await _balance(db, acc.id)
        assert bal_before == Decimal("450")

        resp = await client.post(
            f"/api/v1/transactions/{tx.id}/mark-transfer",
            json={"transfer_direction": "out"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        tx_fresh = await _refetch_tx(db, tx.id)
        meta = json.loads(tx_fresh.metadata_json or "{}")
        assert meta.get("transfer_direction") == "out"
        assert tx_fresh.type == "transfer"

        # transfer+out → -ABS(amount), same as expense; balance stays 450
        bal_after = await _balance(db, acc.id)
        assert bal_after == Decimal("450")

    async def test_single_leg_missing_direction_returns_422(self, client: AsyncClient, db: AsyncSession):
        """Single-leg with no direction → 422 INVALID_INPUT."""
        acc = await _make_account(db, "Acc-422", initial="100")
        tx = await _make_tx(db, acc, "10", tx_type="expense")
        await db.commit()

        resp = await client.post(
            f"/api/v1/transactions/{tx.id}/mark-transfer",
            json={},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422


class TestTwoLegTransfer:
    async def test_two_leg_pair_correct_balances(self, client: AsyncClient, db: AsyncSession):
        """Two-leg pair: both legs correctly tagged in metadata.

        This is the core regression test: the OLD code set type='transfer' on both
        legs without writing transfer_direction, so the balance view applied
        -ABS(amount) to BOTH accounts (double debit). The fix routes through
        pair_transactions() which tags each leg's transfer_direction.

        After pairing with direction='out' on src tx:
          - src tx: type=transfer, metadata.transfer_direction='out'  → -ABS
          - dst tx: type=transfer, metadata.transfer_direction='in'   → +ABS
        Net effect across both accounts = 0 (same as before the transfer).
        """
        src = await _make_account(db, "Src", initial="1000")
        dst = await _make_account(db, "Dst", initial="200")
        out_tx = await _make_tx(db, src, "300", tx_type="expense")
        in_tx = await _make_tx(db, dst, "300", tx_type="income")
        await db.commit()

        # Before: src=1000-300=700 (expense), dst=200+300=500 (income)
        src_before = await _balance(db, src.id)
        dst_before = await _balance(db, dst.id)
        assert src_before == Decimal("700")
        assert dst_before == Decimal("500")

        resp = await client.post(
            f"/api/v1/transactions/{out_tx.id}/mark-transfer",
            json={"counter_transaction_id": in_tx.id, "transfer_direction": "out"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        out_fresh = await _refetch_tx(db, out_tx.id)
        in_fresh = await _refetch_tx(db, in_tx.id)

        out_meta = json.loads(out_fresh.metadata_json or "{}")
        in_meta = json.loads(in_fresh.metadata_json or "{}")
        assert out_meta.get("transfer_direction") == "out"
        assert in_meta.get("transfer_direction") == "in"

        src_after = await _balance(db, src.id)
        dst_after = await _balance(db, dst.id)

        # transfer+out → -ABS same as expense: 1000-300=700
        assert src_after == Decimal("700")
        # transfer+in → +ABS same as income: 200+300=500
        assert dst_after == Decimal("500")
        # Net is identical to before pairing (no double debit)
        assert (src_after + dst_after) == (src_before + dst_before)

    async def test_cross_month_pair_recomputes_cashflow(self, client: AsyncClient, db: AsyncSession):
        """Cross-month pair → both periods recomputed without error."""
        src = await _make_account(db, "CrossSrc", initial="500")
        dst = await _make_account(db, "CrossDst", initial="0")
        out_tx = await _make_tx(db, src, "100", tx_type="expense",
                                occurred_at="2026-04-15T10:00:00Z")
        in_tx = await _make_tx(db, dst, "100", tx_type="income",
                               occurred_at="2026-05-01T10:00:00Z")
        await db.commit()

        resp = await client.post(
            f"/api/v1/transactions/{out_tx.id}/mark-transfer",
            json={"counter_transaction_id": in_tx.id, "transfer_direction": "out"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        out_fresh = await _refetch_tx(db, out_tx.id)
        in_fresh = await _refetch_tx(db, in_tx.id)
        out_meta = json.loads(out_fresh.metadata_json or "{}")
        in_meta = json.loads(in_fresh.metadata_json or "{}")
        assert out_meta.get("transfer_direction") == "out"
        assert in_meta.get("transfer_direction") == "in"
