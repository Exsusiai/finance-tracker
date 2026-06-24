"""PDF import staging flow (2026-06): preview-before-commit.

Verifies the contract the user asked for:
  - upload PARSES + STAGES (status=awaiting_review) and inserts NOTHING
  - commit inserts the transactions + flips status to success
  - cancel/delete removes the staged import (and would remove its txs)

The parser is monkeypatched so the test needs neither a real PDF nor
pdfplumber; the file body just has to pass the %PDF- magic-byte guard.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.core.auth import require_auth  # noqa: E402
from app.db import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, PdfImport, Transaction  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_FAKE_PARSE = {
    "detected_bank": "n26",
    "parser_version": "test",
    "statement_period": "2026-05",
    "raw_text": "fake",
    "error": None,
    "transactions": [
        {"occurred_at": "2026-05-03T00:00:00Z", "amount": "12.50", "currency": "EUR",
         "type": "expense", "description": "Coffee", "external_id": "n26_1_abc"},
        {"occurred_at": "2026-05-04T00:00:00Z", "amount": "2000.00", "currency": "EUR",
         "type": "income", "description": "Salary", "external_id": "n26_2_abc"},
    ],
}


@pytest_asyncio.fixture(autouse=True)
async def _wire(monkeypatch):
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _override_db():
        # Mirror the real get_db: commit on success so each request's writes
        # are visible to the next (otherwise the staged import vanishes).
        async with _Session() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_auth] = lambda: "test"

    async def _fake_parse(db, pdf_import, content, **kwargs):
        return dict(_FAKE_PARSE)

    monkeypatch.setattr(
        "app.services.pdf_parser.engine.parse_pdf_statement", _fake_parse
    )
    # ingestion touches cashflow recompute — keep it (works on the in-mem DB).
    yield
    app.dependency_overrides.clear()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _make_account(name: str = "N26") -> int:
    async with _Session() as s:
        acc = Account(
            name=name, type="bank", currency="EUR", initial_balance=0,
            is_active=True, created_at=_utcnow(), updated_at=_utcnow(),
        )
        s.add(acc)
        await s.commit()
        return acc.id


async def _count_txs() -> int:
    async with _Session() as s:
        return (await s.execute(
            select(func.count(Transaction.id))
        )).scalar() or 0


@pytest_asyncio.fixture()
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestStagingFlow:
    async def test_upload_stages_without_inserting(self, client):
        await _make_account()
        files = {"file": ("n26.pdf", b"%PDF-1.4 fake", "application/pdf")}
        r = await client.post("/api/v1/statements/upload", files=files)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["status"] == "awaiting_review"
        assert data["transactions_count"] == 2
        assert len(data["parsed_preview"]) == 2  # full preview returned
        # NOTHING inserted yet
        assert await _count_txs() == 0

    async def test_commit_inserts(self, client):
        acc_id = await _make_account()
        files = {"file": ("n26.pdf", b"%PDF-1.4 fake", "application/pdf")}
        up = (await client.post("/api/v1/statements/upload", files=files)).json()["data"]
        assert await _count_txs() == 0

        r = await client.post(
            f"/api/v1/statements/{up['id']}/commit?account_id={acc_id}"
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "success"
        assert await _count_txs() == 2  # now inserted

    async def test_cancel_leaves_no_trace(self, client):
        await _make_account()
        files = {"file": ("n26.pdf", b"%PDF-1.4 fake", "application/pdf")}
        up = (await client.post("/api/v1/statements/upload", files=files)).json()["data"]

        r = await client.request("DELETE", f"/api/v1/statements/{up['id']}")
        assert r.status_code == 200, r.text
        # import record gone, no transactions
        async with _Session() as s:
            remaining = (await s.execute(
                select(func.count(PdfImport.id))
            )).scalar() or 0
        assert remaining == 0
        assert await _count_txs() == 0

    async def test_commit_requires_account(self, client):
        # No account exists → candidate is None → commit must 4xx, not crash.
        files = {"file": ("n26.pdf", b"%PDF-1.4 fake", "application/pdf")}
        up = (await client.post("/api/v1/statements/upload", files=files)).json()["data"]
        r = await client.post(f"/api/v1/statements/{up['id']}/commit")
        assert r.status_code >= 400
        assert await _count_txs() == 0
