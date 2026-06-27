"""Tests for transaction split / unsplit (AA / 代付 use case).

Splitting a €100 group-dinner expense into €20 餐饮 + €80 借出 must:
- preserve the account balance exactly (sum of lines == original),
- count only €20 as expense (借出 is transfer-kind),
- be reversible via /unsplit.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "splitsplitsplitsplitsplitsplitsplitsplit"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, Transaction  # noqa: E402

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
        from sqlalchemy import text as _t

        from app.main import _BALANCE_VIEW_DROP_SQL, _BALANCE_VIEW_SQL
        await conn.execute(_t(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(_t(_BALANCE_VIEW_SQL))
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


async def _balance(account_id: int) -> Decimal:
    from sqlalchemy import text
    async with _Session() as s:
        row = (await s.execute(
            text("SELECT balance FROM v_account_balance WHERE account_id=:a"),
            {"a": account_id},
        )).first()
    return Decimal(str(row[0])) if row and row[0] is not None else Decimal("0")


async def _setup() -> tuple[int, int, int, int]:
    """Returns (account_id, dining_cat_id, loan_cat_id, tx_id) for a €100 expense."""
    async with _Session() as s:
        acc = Account(name="PayPal", type="bank", currency="EUR",
                      initial_balance=Decimal("0"), is_active=True,
                      created_at=_utcnow(), updated_at=_utcnow())
        dining = Category(name="餐饮", kind="expense", is_system=False, sort_order=0,
                          created_at=_utcnow(), updated_at=_utcnow())
        loan = Category(name="借出", kind="transfer", is_system=False, sort_order=0,
                        created_at=_utcnow(), updated_at=_utcnow())
        s.add_all([acc, dining, loan])
        await s.flush()
        tx = Transaction(account_id=acc.id, category_id=dining.id,
                         occurred_at="2026-05-10T12:00:00Z", amount=Decimal("100"),
                         currency="EUR", type="expense", source="pdf_import",
                         is_pending=False, created_at=_utcnow(), updated_at=_utcnow())
        s.add(tx)
        await s.commit()
        return acc.id, dining.id, loan.id, tx.id


@pytest.mark.asyncio
async def test_split_preserves_balance_and_counts_only_share(client: AsyncClient):
    acc_id, dining, loan, tx_id = await _setup()
    assert await _balance(acc_id) == Decimal("-100")

    resp = await client.post(f"/api/v1/transactions/{tx_id}/split", headers=AUTH, json={
        "lines": [
            {"amount": "20", "type": "expense", "category_id": dining, "description": "我的份额"},
            {"amount": "80", "type": "transfer", "category_id": loan, "description": "替4人垫付"},
        ],
    })
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert len(rows) == 2
    # Balance unchanged (20 expense + 80 transfer-out = -100).
    assert await _balance(acc_id) == Decimal("-100")

    async with _Session() as s:
        active = (await s.execute(
            select(Transaction).where(Transaction.account_id == acc_id,
                                      Transaction.deleted_at.is_(None))
        )).scalars().all()
        assert len(active) == 2
        expenses = [t for t in active if t.type == "expense"]
        transfers = [t for t in active if t.type == "transfer"]
        assert len(expenses) == 1 and expenses[0].amount == Decimal("20")
        assert len(transfers) == 1 and transfers[0].amount == Decimal("80")
        # Original keeps id; both tagged with the same split group.
        import json
        for t in active:
            assert json.loads(t.metadata_json)["split_group_id"] == tx_id


@pytest.mark.asyncio
async def test_split_sum_mismatch_rejected(client: AsyncClient):
    acc_id, dining, loan, tx_id = await _setup()
    resp = await client.post(f"/api/v1/transactions/{tx_id}/split", headers=AUTH, json={
        "lines": [
            {"amount": "20", "type": "expense", "category_id": dining},
            {"amount": "70", "type": "transfer", "category_id": loan},  # 90 != 100
        ],
    })
    assert resp.status_code in (400, 422), resp.text
    assert await _balance(acc_id) == Decimal("-100")  # untouched


@pytest.mark.asyncio
async def test_unsplit_restores_original(client: AsyncClient):
    acc_id, dining, loan, tx_id = await _setup()
    await client.post(f"/api/v1/transactions/{tx_id}/split", headers=AUTH, json={
        "lines": [
            {"amount": "20", "type": "expense", "category_id": dining},
            {"amount": "80", "type": "transfer", "category_id": loan},
        ],
    })
    resp = await client.post(f"/api/v1/transactions/{tx_id}/unsplit", headers=AUTH)
    assert resp.status_code == 200, resp.text
    restored = resp.json()["data"]
    assert restored["amount"] == "100"
    assert restored["type"] == "expense"
    assert restored["category_id"] == dining

    async with _Session() as s:
        active = (await s.execute(
            select(Transaction).where(Transaction.account_id == acc_id,
                                      Transaction.deleted_at.is_(None))
        )).scalars().all()
        assert len(active) == 1  # siblings removed
        import json
        assert json.loads(active[0].metadata_json or "{}").get("split_group_id") is None
    assert await _balance(acc_id) == Decimal("-100")
