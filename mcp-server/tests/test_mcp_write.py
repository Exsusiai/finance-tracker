"""MCP WRITE tools go through the backend's shared services (V9-R1).

The old hand-written sync sqlite3 mirror drifted from REST behaviour
repeatedly (V6–V9). These tests pin the converged behaviour: the same
write-entrance invariants as the REST routes (kind match, snapshot-account
guard, currency-unit guard), the shared ingestion pipeline (sign
normalisation), and atomic failure for statement imports.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))
sys.path.insert(0, str(_HERE.parent / "backend"))

import app.db as app_db  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.models import Account, Category, Transaction  # noqa: E402
from finance_mcp import server  # noqa: E402


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(app_db, "async_session_factory", sessions)
    yield sessions
    await engine.dispose()


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(sessions, name="Bank", kind="bank", currency="EUR") -> int:
    async with sessions() as s:
        a = Account(
            name=name, type=kind, currency=currency, initial_balance=Decimal("0"),
            is_active=True, created_at=_utcnow(), updated_at=_utcnow(),
        )
        s.add(a)
        await s.commit()
        return a.id


@pytest.mark.asyncio
async def test_add_transaction_normalizes_sign_via_shared_pipeline(env):
    aid = await _make_account(env)
    res = await server.add_transaction(
        account_id=aid, amount="-42.50", currency="EUR", type="expense",
        occurred_at="2026-09-10T00:00:00Z", description="mcp expense",
        counterparty=None, category_id=None, tags=None, is_pending=False,
    )
    assert res["success"] is True, res
    assert res["data"]["amount"] == "42.50", "ingestion must store ABS(amount)"
    assert res["data"]["source"] == "mcp_agent"
    async with env() as s:
        row = await s.get(Transaction, res["data"]["id"])
        assert row is not None and row.amount == Decimal("42.50")


@pytest.mark.asyncio
async def test_add_transaction_rejects_snapshot_account(env):
    aid = await _make_account(env, name="IBKR", kind="brokerage", currency="USDT")
    res = await server.add_transaction(
        account_id=aid, amount="100", currency="USDT", type="expense",
        occurred_at=None, description=None, counterparty=None,
        category_id=None, tags=None, is_pending=False,
    )
    assert res["success"] is False
    assert "holdings" in res["error"], res


@pytest.mark.asyncio
async def test_add_transaction_rejects_currency_mismatch(env):
    aid = await _make_account(env, currency="EUR")
    res = await server.add_transaction(
        account_id=aid, amount="100", currency="USD", type="expense",
        occurred_at=None, description=None, counterparty=None,
        category_id=None, tags=None, is_pending=False,
    )
    assert res["success"] is False
    assert "币种" in res["error"], res


@pytest.mark.asyncio
async def test_add_transaction_rejects_kind_mismatch(env):
    aid = await _make_account(env)
    async with env() as s:
        cat = Category(name="SalaryMCP", kind="income",
                       created_at=_utcnow(), updated_at=_utcnow())
        s.add(cat)
        await s.commit()
        cat_id = cat.id
    res = await server.add_transaction(
        account_id=aid, amount="10", currency="EUR", type="expense",
        occurred_at=None, description=None, counterparty=None,
        category_id=cat_id, tags=None, is_pending=False,
    )
    assert res["success"] is False
    assert "kind" in res["error"], res


@pytest.mark.asyncio
async def test_parse_failure_is_atomic_and_retryable(env, tmp_path, monkeypatch):
    """Mid-batch failure rolls the WHOLE import back (no partial rows), records
    a failed import, and a retry after the cause is fixed succeeds — the failed
    record must not trip the duplicate-hash guard (V9-F17)."""
    from types import SimpleNamespace

    from sqlalchemy import select, text as sqltext

    from app.services.pdf_parser import engine as parser_engine

    aid = await _make_account(env)
    monkeypatch.setattr(
        server, "settings", SimpleNamespace(pdf_storage_dir=tmp_path / "pdfs"),
    )
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.7\nmcp-write-test")

    calls = {"n": 0}

    async def fake_parse(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Second row violates the transactions.type CHECK constraint.
            return {"detected_bank": "test", "transactions": [
                {"occurred_at": "2026-09-10T00:00:00Z", "amount": "100",
                 "currency": "EUR", "type": "expense"},
                {"occurred_at": "2026-09-10T00:00:00Z", "amount": "200",
                 "currency": "EUR", "type": "invalid"},
            ]}
        return {"detected_bank": "test", "transactions": [
            {"occurred_at": "2026-09-10T00:00:00Z", "amount": "100",
             "currency": "EUR", "type": "expense"},
        ]}

    monkeypatch.setattr(parser_engine, "parse_pdf_statement", fake_parse)

    first = await server.parse_bank_statement(str(pdf), account_id=aid, auto_confirm=False)
    assert first["success"] is False, first
    async with env() as s:
        count = (await s.execute(
            sqltext("SELECT COUNT(*) FROM transactions WHERE deleted_at IS NULL")
        )).scalar_one()
        assert count == 0, f"failed import committed {count} partial row(s)"

    second = await server.parse_bank_statement(str(pdf), account_id=aid, auto_confirm=True)
    assert second["success"] is True, second
    assert second["data"]["transactions_count"] == 1
    async with env() as s:
        from app.models import PdfImport
        rows = (await s.execute(select(PdfImport))).scalars().all()
        assert [r.status for r in rows] == ["success"], (
            "the stale failed record must be purged on retry"
        )
