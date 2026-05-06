"""Pre-UAT engineering audit tests (Sprint 4 final).

Verifies the 12 risk scenarios identified during the pre-UAT walk-through
of the bookkeeping domain. Each test pins down a behavior we've manually
reasoned about so it can't regress silently.

Risk reference (R# from the audit checklist):
- R1  PATCH 跨月 cleans old period
- R2  PATCH 跨 type savings flips sign
- R3  DELETE recomputes its period
- R4  cashflow income/expense never includes transfer rows
- R5  subaccount transfer is excluded from cashflow.transfer_total
- R6  adjustment is excluded from income/expense and only feeds other_total
- R7  mark-transfer single-leg → two-leg upgrade overwrites direction correctly
- R8  matcher refuses to pair an already-paired tx (idempotence)
- R9  ingest_transactions Step 1.5 is FX-fold idempotent (caller-supplied
      base_amount preserved)  — already covered by test_multi_currency_ingestion
- R10 PATCH currency-only refolds even when amount unchanged
- R11 category DELETE doesn't break related transactions (FK SET NULL)
- R12 soft-deleted txs don't contribute to cashflow snapshots
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL, app  # noqa: E402
from app.models import Account, Category, FxRate  # noqa: E402

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


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_schema():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))
    async with _TestingSessionLocal() as s:
        s.add_all([
            Account(
                id=1, name="Checking", type="bank", currency="CNY",
                initial_balance=Decimal("0"), is_active=True,
                created_at="2026-05-01T00:00:00Z", updated_at="2026-05-01T00:00:00Z",
            ),
            Account(
                id=2, name="Savings", type="bank", currency="CNY",
                initial_balance=Decimal("0"), is_active=True,
                created_at="2026-05-01T00:00:00Z", updated_at="2026-05-01T00:00:00Z",
            ),
            Category(
                id=1, name="Food", kind="expense", is_system=False, sort_order=0,
                created_at="2026-05-01T00:00:00Z",
            ),
            Category(
                id=2, name="Salary", kind="income", is_system=False, sort_order=0,
                created_at="2026-05-01T00:00:00Z",
            ),
        ])
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


def _make_tx_body(**overrides):
    base = {
        "account_id": 1,
        "occurred_at": "2026-05-10T00:00:00Z",
        "amount": "100.00",
        "currency": "CNY",
        "type": "expense",
    }
    base.update(overrides)
    return base


async def _snapshot(period: str) -> dict:
    """Read cash_flow_snapshots row for `YYYY-MM`."""
    year, month = int(period[:4]), int(period[5:7])
    async with _TestingSessionLocal() as s:
        row = (await s.execute(
            text("SELECT income_total, expense_total, transfer_total, savings_total, other_total "
                 "FROM cash_flow_snapshots WHERE period_year=:y AND period_month=:m"),
            {"y": year, "m": month},
        )).first()
        if not row:
            return {}
        return {
            "income": float(row[0]),
            "expense": float(row[1]),
            "transfer": float(row[2]),
            "savings": float(row[3]),
            "other": float(row[4]),
        }


# ─── R1: PATCH 跨月 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_r1_patch_cross_month_clears_old_period(client: AsyncClient) -> None:
    """PATCH that moves occurred_at to a different month must rewrite both
    snapshots — old month should drop the row, new month should pick it up."""
    create = await client.post(
        "/api/v1/transactions",
        json=_make_tx_body(occurred_at="2026-04-15T00:00:00Z", amount="200.00"),
        headers=AUTH,
    )
    assert create.status_code == 201
    tx_id = create.json()["data"]["id"]

    snap_april = await _snapshot("2026-04")
    assert snap_april.get("expense") == 200.0

    # Move it to May
    r = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"occurred_at": "2026-05-15T00:00:00Z"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    snap_april2 = await _snapshot("2026-04")
    snap_may = await _snapshot("2026-05")
    assert snap_april2.get("expense", 0) == 0.0, "old month must drop the row"
    assert snap_may.get("expense") == 200.0, "new month must include the row"


# ─── R2: PATCH 跨 type savings sign flip ────────────────────────────────


@pytest.mark.asyncio
async def test_r2_patch_type_flip_changes_savings_sign(client: AsyncClient) -> None:
    """A 100 expense flipping to a 100 income should swing savings by 200
    (from -100 to +100)."""
    create = await client.post(
        "/api/v1/transactions",
        json=_make_tx_body(occurred_at="2026-06-10T00:00:00Z", amount="100.00", type="expense"),
        headers=AUTH,
    )
    tx_id = create.json()["data"]["id"]

    snap = await _snapshot("2026-06")
    initial_savings = snap.get("savings", 0)
    initial_expense = snap.get("expense", 0)

    r = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"type": "income"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    snap2 = await _snapshot("2026-06")
    # savings flipped from negative-of-amount to positive-of-amount → swing 2x
    assert (snap2.get("savings", 0) - initial_savings) == 200.0
    assert snap2.get("expense", 0) == initial_expense - 100.0
    assert snap2.get("income", 0) == 100.0


# ─── R3: DELETE recomputes period ───────────────────────────────────────


@pytest.mark.asyncio
async def test_r3_delete_recomputes_period(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/transactions",
        json=_make_tx_body(occurred_at="2026-07-10T00:00:00Z", amount="333.33"),
        headers=AUTH,
    )
    tx_id = create.json()["data"]["id"]

    snap = await _snapshot("2026-07")
    assert snap.get("expense") == 333.33

    r = await client.delete(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    assert r.status_code == 200, r.text

    snap2 = await _snapshot("2026-07")
    assert snap2.get("expense", 0) == 0.0


# ─── R4 + R5 + R6: cashflow type filtering ───────────────────────────────


@pytest.mark.asyncio
async def test_r4_r5_r6_cashflow_type_filtering(client: AsyncClient) -> None:
    """Verify in one period:
    - transfer rows do NOT enter income_total or expense_total (R4)
    - subaccount-tagged transfer rows do NOT enter transfer_total (R5)
    - adjustment rows do NOT enter income/expense, only other_total (R6)"""
    period = "2026-08"
    base = _make_tx_body(occurred_at=f"{period}-10T00:00:00Z")

    # 1 expense + 1 income
    await client.post("/api/v1/transactions", json={**base, "amount": "100", "type": "expense"}, headers=AUTH)
    await client.post("/api/v1/transactions", json={**base, "amount": "50", "type": "income"}, headers=AUTH)
    # 1 normal transfer (between Checking and Savings)
    await client.post("/api/v1/transactions", json={
        **base, "amount": "30", "type": "transfer",
        "metadata_json": '{"transfer_direction": "out"}',
    }, headers=AUTH)
    # 1 subaccount transfer (in-bank move) — should NOT count
    await client.post("/api/v1/transactions", json={
        **base, "amount": "999", "type": "transfer",
        "metadata_json": '{"subaccount": true, "transfer_direction": "out"}',
    }, headers=AUTH)
    # 1 adjustment
    await client.post("/api/v1/transactions", json={
        **base, "amount": "5", "type": "adjustment",
    }, headers=AUTH)

    snap = await _snapshot(period)
    # R4: income 50, expense 100 — transfer rows excluded
    assert snap.get("income") == 50.0
    assert snap.get("expense") == 100.0
    # R5: transfer_total only counts the non-subaccount transfer
    assert snap.get("transfer") == 30.0, (
        f"subaccount transfer must be excluded from transfer_total, got {snap.get('transfer')}"
    )
    # R6: adjustment lands in other_total only
    assert snap.get("other") == 5.0
    # savings = income - expense (transfer/adjustment ignored)
    assert snap.get("savings") == -50.0


# ─── R7: mark-transfer single-leg → two-leg upgrade ─────────────────────


@pytest.mark.asyncio
async def test_r7_mark_transfer_single_then_pair(client: AsyncClient) -> None:
    """First mark a tx single-leg, then upgrade by passing counter id.
    The second call must overwrite direction correctly + cross-link."""
    create_a = await client.post(
        "/api/v1/transactions",
        json={**_make_tx_body(occurred_at="2026-09-01T00:00:00Z", amount="200"), "type": "expense"},
        headers=AUTH,
    )
    create_b = await client.post(
        "/api/v1/transactions",
        json={**_make_tx_body(occurred_at="2026-09-02T00:00:00Z", amount="200", account_id=2), "type": "income"},
        headers=AUTH,
    )
    a_id = create_a.json()["data"]["id"]
    b_id = create_b.json()["data"]["id"]

    # Single-leg first (just A as outgoing).
    r1 = await client.post(
        f"/api/v1/transactions/{a_id}/mark-transfer",
        json={"transfer_direction": "out"},
        headers=AUTH,
    )
    assert r1.status_code == 200, r1.text

    # Now upgrade with counter.
    r2 = await client.post(
        f"/api/v1/transactions/{a_id}/mark-transfer",
        json={"counter_transaction_id": b_id, "transfer_direction": "out"},
        headers=AUTH,
    )
    assert r2.status_code == 200, r2.text

    # A should be type=transfer, direction=out, counter_account_id=2
    g_a = await client.get(f"/api/v1/transactions/{a_id}", headers=AUTH)
    g_b = await client.get(f"/api/v1/transactions/{b_id}", headers=AUTH)
    a = g_a.json()["data"]
    b = g_b.json()["data"]
    assert a["type"] == "transfer"
    assert b["type"] == "transfer"
    assert a["counter_account_id"] == 2
    assert b["counter_account_id"] == 1
    import json as _json
    assert _json.loads(a["metadata_json"]).get("transfer_direction") == "out"
    assert _json.loads(b["metadata_json"]).get("transfer_direction") == "in"


# ─── R8: matcher refuses to re-pair already-paired tx ───────────────────


@pytest.mark.asyncio
async def test_r8_already_paired_tx_skipped() -> None:
    from app.models import Transaction
    from app.services.transfer_matcher.engine import _is_eligible

    paired = Transaction(
        type="transfer",
        counter_account_id=42,  # already paired
        deleted_at=None,
    )
    sub = Transaction(
        type="transfer",
        counter_account_id=None,
        metadata_json='{"subaccount": true}',
        deleted_at=None,
    )
    fresh = Transaction(
        type="expense",
        counter_account_id=None,
        deleted_at=None,
    )
    assert _is_eligible(paired) is False, "already-paired transfer must be skipped"
    assert _is_eligible(sub) is False, "subaccount move must be skipped"
    assert _is_eligible(fresh) is True, "untagged expense should be matchable"


# ─── R10: PATCH currency-only refolds ───────────────────────────────────


@pytest.mark.asyncio
async def test_r10_patch_currency_only_refolds(client: AsyncClient) -> None:
    """If user changes only `currency` (amount unchanged), old base_amount
    is stale and must be cleared + recomputed via ingestion's FX fold."""
    # Seed an EUR→CNY rate of 8.
    async with _TestingSessionLocal() as s:
        s.add(FxRate(
            base_currency="EUR", quote_currency="CNY", rate=Decimal("8.0"),
            quoted_at="2026-10-01T00:00:00Z", source="test",
        ))
        await s.commit()

    # Create a CNY tx (same currency → no FX needed).
    create = await client.post(
        "/api/v1/transactions",
        json=_make_tx_body(occurred_at="2026-10-10T00:00:00Z", amount="50", currency="CNY"),
        headers=AUTH,
    )
    tx_id = create.json()["data"]["id"]

    # Now PATCH only currency=EUR. amount stays 50.
    # Expected: base_amount becomes 400 (50 EUR * 8 CNY/EUR).
    r = await client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"currency": "EUR"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    g = await client.get(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    data = g.json()["data"]
    assert data["currency"] == "EUR"
    assert data["base_amount"] == "400" or data["base_amount"] == "400.0", (
        f"PATCH currency must trigger FX refold, got base_amount={data['base_amount']!r}"
    )

    # cashflow now reads 400 (folded), not 50
    snap = await _snapshot("2026-10")
    assert snap.get("expense") == 400.0


# ─── R11: category DELETE leaves transactions intact ────────────────────


@pytest.mark.asyncio
async def test_r11_category_delete_sets_null_on_transactions(client: AsyncClient) -> None:
    # Create a sacrificial category we can actually delete (not is_system).
    create_cat = await client.post(
        "/api/v1/categories",
        json={"name": "Throwaway", "kind": "expense"},
        headers=AUTH,
    )
    cat_id = create_cat.json()["data"]["id"]

    create_tx = await client.post(
        "/api/v1/transactions",
        json={**_make_tx_body(occurred_at="2026-11-10T00:00:00Z"), "category_id": cat_id},
        headers=AUTH,
    )
    tx_id = create_tx.json()["data"]["id"]

    # Delete category.
    r = await client.delete(f"/api/v1/categories/{cat_id}", headers=AUTH)
    assert r.status_code == 200, r.text

    # Transaction still exists; category_id is NULL via FK SET NULL.
    g = await client.get(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    assert g.status_code == 200
    data = g.json()["data"]
    assert data["category_id"] is None, "FK ON DELETE SET NULL should null out category_id"


# ─── R12: soft-deleted tx excluded from cashflow ────────────────────────


@pytest.mark.asyncio
async def test_r12_soft_deleted_excluded_from_cashflow(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/transactions",
        json=_make_tx_body(occurred_at="2026-12-10T00:00:00Z", amount="77.77"),
        headers=AUTH,
    )
    tx_id = create.json()["data"]["id"]
    snap = await _snapshot("2026-12")
    assert snap.get("expense") == 77.77

    await client.delete(f"/api/v1/transactions/{tx_id}", headers=AUTH)
    snap2 = await _snapshot("2026-12")
    assert snap2.get("expense", 0) == 0.0
