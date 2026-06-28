"""Tests for PDF closing-balance extraction, reconciliation, and anchoring.

- Each bank parser extracts the statement's end-of-period balance.
- compute_reconciliation compares it to the computed ledger balance.
- anchor_account_balance back-solves initial_balance so the curve hits reality.
- POST /accounts/{id}/anchor-balance persists it; snapshot accounts rejected.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "anchoranchoranchoranchoranchoranchoran"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Transaction  # noqa: E402

_SAMPLES = Path(__file__).resolve().parents[2] / "data" / "inputpdf_reference"

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}", "Content-Type": "application/json"}


async def _override_get_db():
    async with _Session() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _tables():
    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.pop(get_db, None) if prev is None else app.dependency_overrides.__setitem__(get_db, prev)


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Closing-balance extraction (real sample PDFs) ──────────────────────

@pytest.mark.skipif(not _SAMPLES.exists(), reason="sample PDFs not available")
@pytest.mark.parametrize("name,bank,expected", [
    ("N26", "n26", Decimal("1622.26")),          # main + Spaces summed
    ("AMEX-DE", "amex_de", Decimal("548.72")),   # printed positive (owed)
    ("TFBank", "tfbank", Decimal("-855.87")),    # printed negative
    ("advanzia", "advanzia", Decimal("371.41")),
    ("Revolut", "revolut", None),                # positional column → skipped
])
def test_extract_closing_balance(name, bank, expected):
    import pdfplumber
    from app.services.pdf_parser.engine import extract_closing_balance

    pdf = _SAMPLES / f"{name}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf} missing")
    txt = ""
    with pdfplumber.open(str(pdf)) as doc:
        for p in doc.pages:
            txt += (p.extract_text() or "") + "\n"
    got = extract_closing_balance(bank, txt)
    assert got == expected, f"{name}: got {got}, expected {expected}"


# ─── Reconciliation + anchor (service layer) ────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_and_anchor_asset_account():
    from app.services.valuation.anchor import anchor_account_balance, compute_reconciliation

    async with _Session() as s:
        acc = Account(name="Bank", type="bank", currency="EUR",
                      initial_balance=Decimal("0"), is_active=True,
                      created_at=_utcnow(), updated_at=_utcnow())
        s.add(acc); await s.flush()
        s.add_all([
            Transaction(account_id=acc.id, occurred_at="2026-03-01T00:00:00Z",
                        amount=Decimal("1000"), currency="EUR", type="income",
                        source="pdf_import", is_pending=True, created_at=_utcnow(), updated_at=_utcnow()),
            Transaction(account_id=acc.id, occurred_at="2026-04-01T00:00:00Z",
                        amount=Decimal("300"), currency="EUR", type="expense",
                        source="pdf_import", is_pending=True, created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await s.commit()
        acc_id = acc.id

    as_of = "2026-04-30T23:59:59Z"
    # Before anchor: ledger = 0 + (1000 − 300) = 700; statement says 1000 → off by 300.
    async with _Session() as s:
        acc = (await s.execute(select(Account).where(Account.id == acc_id))).scalar_one()
        rec = await compute_reconciliation(s, acc, Decimal("1000"), as_of)
        assert Decimal(rec["computed_balance"]) == Decimal("700")
        assert Decimal(rec["closing_balance"]) == Decimal("1000")
        assert Decimal(rec["discrepancy"]) == Decimal("300")
        assert rec["previously_anchored"] is False  # first time

    # Anchor to the statement → initial becomes 300, ledger now hits 1000.
    async with _Session() as s:
        acc = (await s.execute(select(Account).where(Account.id == acc_id))).scalar_one()
        new_initial = await anchor_account_balance(s, acc, Decimal("1000"), as_of)
        await s.commit()
        assert new_initial == Decimal("300")

    async with _Session() as s:
        acc = (await s.execute(select(Account).where(Account.id == acc_id))).scalar_one()
        rec = await compute_reconciliation(s, acc, Decimal("1000"), as_of)
        assert Decimal(rec["discrepancy"]) == Decimal("0")  # reconciles
        assert rec["previously_anchored"] is True  # now anchored → drift detection mode
        bal = (await s.execute(
            text("SELECT balance FROM v_account_balance WHERE account_id=:a"), {"a": acc_id}
        )).scalar()
        assert Decimal(str(bal)) == Decimal("1000")


@pytest.mark.asyncio
async def test_reconcile_credit_card_sign():
    """A credit card prints owed=371.41 (positive); ledger stores debt negative.
    normalize_closing maps it to −371.41 so a single 371.41 purchase reconciles."""
    from app.services.valuation.anchor import compute_reconciliation

    async with _Session() as s:
        acc = Account(name="CC", type="credit_card", currency="EUR",
                      initial_balance=Decimal("0"), is_active=True,
                      created_at=_utcnow(), updated_at=_utcnow())
        s.add(acc); await s.flush()
        s.add(Transaction(account_id=acc.id, occurred_at="2026-04-02T00:00:00Z",
                          amount=Decimal("371.41"), currency="EUR", type="expense",
                          source="pdf_import", is_pending=True, created_at=_utcnow(), updated_at=_utcnow()))
        await s.commit()
        acc_id = acc.id

    async with _Session() as s:
        acc = (await s.execute(select(Account).where(Account.id == acc_id))).scalar_one()
        rec = await compute_reconciliation(s, acc, Decimal("371.41"), "2026-04-30T00:00:00Z")
        assert Decimal(rec["closing_balance"]) == Decimal("-371.41")
        assert Decimal(rec["computed_balance"]) == Decimal("-371.41")
        assert Decimal(rec["discrepancy"]) == Decimal("0")


@pytest.mark.asyncio
async def test_anchor_endpoint_and_snapshot_rejected(client: AsyncClient):
    async with _Session() as s:
        bank = Account(name="EndpBank", type="bank", currency="EUR",
                       initial_balance=Decimal("0"), is_active=True,
                       created_at=_utcnow(), updated_at=_utcnow())
        broker = Account(name="Broker", type="brokerage", currency="EUR",
                         initial_balance=Decimal("0"), is_active=True,
                         created_at=_utcnow(), updated_at=_utcnow())
        s.add_all([bank, broker]); await s.flush()
        s.add(Transaction(account_id=bank.id, occurred_at="2026-05-01T00:00:00Z",
                          amount=Decimal("200"), currency="EUR", type="income",
                          source="manual", is_pending=False, created_at=_utcnow(), updated_at=_utcnow()))
        await s.commit()
        bank_id, broker_id = bank.id, broker.id

    # Anchor bank to 500 as of after the 200 income → initial = 500 − 200 = 300.
    resp = await client.post(f"/api/v1/accounts/{bank_id}/anchor-balance", headers=AUTH,
                             json={"balance": "500", "as_of": "2026-05-31T00:00:00Z"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["initial_balance"] in ("300", "300.00000000")

    # Snapshot accounts can't be anchored.
    resp = await client.post(f"/api/v1/accounts/{broker_id}/anchor-balance", headers=AUTH,
                             json={"balance": "1000", "as_of": "2026-05-31T00:00:00Z"})
    assert resp.status_code == 400, resp.text
