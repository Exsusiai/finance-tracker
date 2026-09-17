"""Regression tests for the V9 transaction-core money bugs (F01/F02/F03/F09/F10/F13/F21).

Each test names the F-number it locks down. They all run against a fresh
in-memory SQLite with the real `v_account_balance` view, so balance assertions
exercise the same signed-amount SQL production uses.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.core.auth import require_auth  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import _BALANCE_VIEW_SQL, _index_migrations, app  # noqa: E402
from app.models import Account, PdfImport, Transaction  # noqa: E402

DATE = "2026-09-10T12:00:00Z"
BASE = get_settings().base_currency
FOREIGN = "USD" if BASE != "USD" else "EUR"


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(_BALANCE_VIEW_SQL))
        for _name, ddl in _index_migrations:
            await conn.execute(text(ddl))

    async def _override_db():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_auth] = lambda: "test"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield sessions, client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    await engine.dispose()


async def _account(s, name="Bank", currency=BASE, kind="bank", initial="0") -> Account:
    acct = Account(
        name=name, currency=currency, type=kind, initial_balance=Decimal(initial)
    )
    s.add(acct)
    await s.flush()
    return acct


async def _tx(s, acct, amount="100", kind="expense", **kw) -> Transaction:
    row = Transaction(
        account_id=acct.id,
        amount=Decimal(amount),
        currency=acct.currency,
        type=kind,
        occurred_at=DATE,
        source="pdf_import",
        is_pending=False,
        **kw,
    )
    s.add(row)
    await s.flush()
    return row


async def _balance(s, account_id: int) -> Decimal:
    value = (await s.execute(
        text("SELECT balance FROM v_account_balance WHERE account_id=:id"),
        {"id": account_id},
    )).scalar_one()
    return Decimal(str(value))


# ─── F01: split must divide the ORIGINAL base_amount ────────────────────


async def test_f01_split_preserves_original_base_amount(env):
    """Each line's base share was computed against the already-shrunk
    base_amount of line 1, so a 90 EUR / 100 USD row split 20/80 booked 32.4."""
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s, currency=FOREIGN)
        tx = await _tx(
            s, acct, base_amount=Decimal("90"), fx_rate_to_base=Decimal("0.9")
        )
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/{tx.id}/split",
        json={
            "lines": [
                {"amount": "20", "type": "expense"},
                {"amount": "80", "type": "expense"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    lines = r.json()["data"]
    assert sum(Decimal(x["base_amount"]) for x in lines) == Decimal("90")
    assert sum(Decimal(x["amount"]) for x in lines) == Decimal("100")


async def test_f01_split_remainder_lands_on_last_line(env):
    """A 3-way split of an indivisible base must still sum back exactly."""
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s, currency=FOREIGN)
        tx = await _tx(
            s, acct, base_amount=Decimal("100"), fx_rate_to_base=Decimal("1")
        )
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/{tx.id}/split",
        json={
            "lines": [
                {"amount": "33.33", "type": "expense"},
                {"amount": "33.33", "type": "expense"},
                {"amount": "33.34", "type": "expense"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    lines = r.json()["data"]
    assert sum(Decimal(x["base_amount"]) for x in lines) == Decimal("100")


# ─── F02: a statement owns the rows its split produced ──────────────────


async def test_f02_statement_delete_removes_split_children(env):
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        pdf = PdfImport(
            filename="test.pdf", file_hash="a" * 64, file_size=8,
            storage_path="", account_id=acct.id, status="success",
        )
        s.add(pdf)
        await s.flush()
        tx = await _tx(s, acct, pdf_import_id=pdf.id)
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/{tx.id}/split",
        json={
            "lines": [
                {"amount": "20", "type": "expense"},
                {"amount": "80", "type": "expense"},
            ]
        },
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"/api/v1/statements/{pdf.id}")
    assert r.status_code == 200, r.text
    async with sessions() as s:
        assert await _balance(s, acct.id) == Decimal("0")


async def test_f02_statement_delete_sweeps_legacy_split_children(env):
    """Siblings created before the fix carry no pdf_import_id — they have to be
    swept by split_group_id or their amount survives as orphan balance."""
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        pdf = PdfImport(
            filename="legacy.pdf", file_hash="b" * 64, file_size=8,
            storage_path="", account_id=acct.id, status="success",
        )
        s.add(pdf)
        await s.flush()
        tx = await _tx(s, acct, pdf_import_id=pdf.id)
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/{tx.id}/split",
        json={
            "lines": [
                {"amount": "20", "type": "expense"},
                {"amount": "80", "type": "expense"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    child_id = r.json()["data"][1]["id"]

    async with sessions() as s:  # degrade the child to its pre-fix shape
        child = await s.get(Transaction, child_id)
        child.pdf_import_id = None
        await s.commit()

    r = await client.delete(f"/api/v1/statements/{pdf.id}")
    assert r.status_code == 200, r.text
    async with sessions() as s:
        assert await _balance(s, acct.id) == Decimal("0")


# ─── F03: a split may not reverse the direction of money ────────────────


async def test_f03_opposite_direction_split_is_rejected(env):
    """20 expense + 80 income sums to 100 in absolute terms but nets +60 on the
    balance — it is not a split of a −100 expense."""
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        tx = await _tx(s, acct)
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/{tx.id}/split",
        json={
            "lines": [
                {"amount": "20", "type": "expense"},
                {"amount": "80", "type": "income"},
            ]
        },
    )
    assert r.status_code in (400, 422), r.text
    async with sessions() as s:
        assert await _balance(s, acct.id) == Decimal("-100")


async def test_f03_transfer_lines_are_still_allowed(env):
    """The AA use case — €20 mine, €80 lent out — must keep working."""
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        tx = await _tx(s, acct)
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/{tx.id}/split",
        json={
            "lines": [
                {"amount": "20", "type": "expense"},
                {"amount": "80", "type": "transfer"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    async with sessions() as s:
        assert await _balance(s, acct.id) == Decimal("-100")


# ─── F09: editing a paired leg must release the pairing ─────────────────


async def test_f09_editing_paired_leg_keeps_monthly_and_timeseries_equal(env):
    """A stale paired_with_tx_id kept feeding paired_dedup_predicate, so the
    edited row vanished from /monthly while /timeseries still counted it."""
    from app.services.transfer_matcher.engine import pair_transactions

    sessions, client = env
    async with sessions() as s:
        alpha, beta = await _account(s, "Alpha"), await _account(s, "Beta")
        out_leg = await _tx(s, alpha)
        in_leg = await _tx(s, beta, kind="income")
        await pair_transactions(s, out_leg, in_leg)
        await s.commit()

    r = await client.patch(
        f"/api/v1/transactions/{in_leg.id}?apply_scope=single",
        json={"type": "income", "category_id": None},
    )
    assert r.status_code == 200, r.text

    monthly = (await client.get("/api/v1/cashflow/monthly")).json()["data"]
    series = (await client.get("/api/v1/cashflow/timeseries")).json()["data"]
    assert Decimal(monthly[0]["income"]) == Decimal(series["income"][0])
    assert Decimal(monthly[0]["income"]) == Decimal("100")

    async with sessions() as s:
        edited = await s.get(Transaction, in_leg.id)
        other = await s.get(Transaction, out_leg.id)
        assert edited.counter_account_id is None
        assert "paired_with_tx_id" not in json.loads(edited.metadata_json or "{}")
        assert other.counter_account_id is None
        assert "paired_with_tx_id" not in json.loads(other.metadata_json or "{}")
        assert other.type == "transfer"  # counterpart stays re-bindable


async def test_f09_category_only_edit_keeps_the_pairing(env):
    """Only type/amount/account/date invalidate a pair — a category edit must
    not silently unbind a correct transfer."""
    from app.services.transfer_matcher.engine import pair_transactions

    sessions, client = env
    async with sessions() as s:
        alpha, beta = await _account(s, "Alpha"), await _account(s, "Beta")
        out_leg = await _tx(s, alpha)
        in_leg = await _tx(s, beta, kind="income")
        await pair_transactions(s, out_leg, in_leg)
        await s.commit()

    r = await client.patch(
        f"/api/v1/transactions/{in_leg.id}?apply_scope=single",
        json={"description": "renamed"},
    )
    assert r.status_code == 200, r.text
    async with sessions() as s:
        still_paired = await s.get(Transaction, in_leg.id)
        meta = json.loads(still_paired.metadata_json or "{}")
        assert meta.get("paired_with_tx_id") == out_leg.id
        assert still_paired.counter_account_id == alpha.id


# ─── F10: inbox confirm must infer the transfer direction ───────────────


async def test_f10_inbox_confirm_income_to_transfer_keeps_direction_in(env):
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        tx = await _tx(s, acct, kind="income")
        tx.is_pending = True
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/inbox/{tx.id}/confirm?apply_scope=single",
        json={"type": "transfer", "category_id": None},
    )
    assert r.status_code == 200, r.text
    async with sessions() as s:
        assert await _balance(s, acct.id) == Decimal("100")
        confirmed = await s.get(Transaction, tx.id)
        assert json.loads(confirmed.metadata_json)["transfer_direction"] == "in"


async def test_f10_inbox_confirm_does_not_override_an_explicit_direction(env):
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        tx = await _tx(
            s, acct, kind="income",
            metadata_json=json.dumps({"transfer_direction": "out"}),
        )
        tx.is_pending = True
        await s.commit()

    r = await client.post(
        f"/api/v1/transactions/inbox/{tx.id}/confirm?apply_scope=single",
        json={"type": "transfer"},
    )
    assert r.status_code == 200, r.text
    async with sessions() as s:
        confirmed = await s.get(Transaction, tx.id)
        assert json.loads(confirmed.metadata_json)["transfer_direction"] == "out"


# ─── F13: an FX-only edit must re-derive base_amount ────────────────────


async def test_f13_fx_only_patch_recomputes_base_amount(env):
    """base_amount outranks fx_rate_to_base downstream, so leaving the old one
    in place made the edited rate a no-op."""
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s, currency=FOREIGN)
        tx = await _tx(
            s, acct, base_amount=Decimal("90"), fx_rate_to_base=Decimal("0.9")
        )
        await s.commit()

    r = await client.patch(
        f"/api/v1/transactions/{tx.id}?apply_scope=single",
        json={"fx_rate_to_base": "0.8"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["data"]["base_amount"]) == Decimal("80")


async def test_f13_contradictory_fx_and_base_amount_is_rejected(env):
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s, currency=FOREIGN)
        tx = await _tx(
            s, acct, base_amount=Decimal("90"), fx_rate_to_base=Decimal("0.9")
        )
        await s.commit()

    r = await client.patch(
        f"/api/v1/transactions/{tx.id}?apply_scope=single",
        json={"fx_rate_to_base": "0.8", "base_amount": "90"},
    )
    assert r.status_code in (400, 422), r.text
    async with sessions() as s:
        unchanged = await s.get(Transaction, tx.id)
        assert unchanged.base_amount == Decimal("90")
        assert unchanged.fx_rate_to_base == Decimal("0.9")


async def test_f13_consistent_fx_and_base_amount_is_accepted(env):
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s, currency=FOREIGN)
        tx = await _tx(
            s, acct, base_amount=Decimal("90"), fx_rate_to_base=Decimal("0.9")
        )
        await s.commit()

    r = await client.patch(
        f"/api/v1/transactions/{tx.id}?apply_scope=single",
        json={"fx_rate_to_base": "0.8", "base_amount": "80"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["data"]["base_amount"]) == Decimal("80")


# ─── F21: literal route must not be eaten by /{transaction_id} ──────────


async def test_f21_recently_promoted_route_is_reachable(env):
    _, client = env
    r = await client.get("/api/v1/transactions/recently-promoted-to-transfer")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


async def test_f21_numeric_transaction_route_still_works(env):
    sessions, client = env
    async with sessions() as s:
        acct = await _account(s)
        tx = await _tx(s, acct)
        await s.commit()

    r = await client.get(f"/api/v1/transactions/{tx.id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["id"] == tx.id


async def test_f21_unknown_literal_segment_still_404s(env):
    _, client = env
    r = await client.get("/api/v1/transactions/not-a-real-route")
    assert r.status_code in (404, 422), r.text
