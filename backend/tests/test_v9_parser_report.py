"""Regression tests for review V9 bugs F18 / F11 / F15.

Each test below is a self-contained port of the corresponding assertion in
`code review/repro_v9.py` (kept read-only per the review protocol), using
this suite's own fixtures/harness conventions instead of importing from it.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_TOKEN = "v9reportv9reportv9reportv9reportv9report"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Asset, AssetHolding, MarketPrice, Transaction  # noqa: E402
from app.services.valuation.composition import compute_composition  # noqa: E402
from app.services.valuation.net_worth import compute_net_worth  # noqa: E402

AUTH_HEADERS = {
    "Authorization": f"Bearer {_TEST_TOKEN}",
    "Content-Type": "application/json",
}

_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def _override_get_db():
    async with _Session() as session:
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
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture()
async def db():
    async with _Session() as session:
        yield session


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── F18: AMEX-DE cross-year statement period ────────────────────────────


def test_f18_amex_january_statement_places_december_in_previous_year() -> None:
    """Regression F18: `_parse_amex_de` grabbed the single header year
    ("Datum 08.01.26" → 2026) and applied it to every row, including rows
    from December that belong to the PREVIOUS year in a period that
    straddles a year boundary ("vom 09.12.25 bis 08.01.26"). Fix: derive
    the row's year from the statement period window instead of the header
    alone.
    """
    from app.services.pdf_parser.engine import _parse_amex_de

    text_ = (
        "Datum 08.01.26\n"
        "Abrechnungszeitraum vom 09.12.25 bis 08.01.26\n"
        "28.12 29.12 GROCERY SHOP 10,00\n"
    )
    rows = _parse_amex_de(text_)
    assert len(rows) == 1, rows
    assert rows[0]["occurred_at"].startswith("2025-12-28"), rows[0]


def test_f18_amex_january_row_within_period_stays_in_header_year() -> None:
    """A row that falls in January (the period's end year) must still land
    in the header year — only the December-side rows need correcting."""
    from app.services.pdf_parser.engine import _parse_amex_de

    text_ = (
        "Datum 08.01.26\n"
        "Abrechnungszeitraum vom 09.12.25 bis 08.01.26\n"
        "03.01 04.01 ONLINE STORE 25,00\n"
    )
    rows = _parse_amex_de(text_)
    assert len(rows) == 1, rows
    assert rows[0]["occurred_at"].startswith("2026-01-03"), rows[0]


# ─── F11: composition dust filter dropped large negative liabilities ─────


async def test_f11_composition_keeps_large_negative_liabilities(db: AsyncSession):
    """Regression F11: the dust filter (`if b.value < _DUST`) treated ANY
    negative value as "below €0.1" and silently dropped it, so a -1000
    credit-card-debt bucket vanished from composition entirely, breaking
    the composition.total == net_worth.net_worth invariant. Fix: dust check
    uses `abs(b.value) < _DUST`.
    """
    debt = Account(name="Debt", type="credit_card", currency="EUR",
                    initial_balance=Decimal("-1000"), is_active=True,
                    include_in_total=True, created_at=_utcnow(), updated_at=_utcnow())
    broker = Account(name="Broker", type="brokerage", currency="EUR",
                      initial_balance=Decimal("0"), is_active=True,
                      include_in_total=True, created_at=_utcnow(), updated_at=_utcnow())
    db.add_all([debt, broker])
    await db.flush()

    asset = Asset(symbol="STOCK", name="Stock", asset_class="us_stock", currency="EUR",
                  created_at=_utcnow(), updated_at=_utcnow())
    db.add(asset)
    await db.flush()
    db.add_all([
        AssetHolding(account_id=broker.id, asset_id=asset.id, quantity=Decimal("10"),
                     chain="", is_active=True, created_at=_utcnow(), updated_at=_utcnow()),
        MarketPrice(asset_id=asset.id, quoted_at=_utcnow(), price=Decimal("1000"),
                    currency="EUR", source="test"),
    ])
    await db.commit()

    nw = await compute_net_worth(db, "EUR")
    composition = await compute_composition(db, "EUR")
    assert composition.total == nw.net_worth, (
        f"composition={composition.total}, net worth={nw.net_worth}, "
        f"dust={composition.dust_excluded_count}"
    )
    by_label = {e["label"]: e for e in composition.entries}
    assert Decimal(by_label["EUR 现金"]["value"]) == Decimal("-1000")
    assert Decimal(nw.net_worth) == Decimal("9000")


async def test_f11_true_dust_is_still_excluded(db: AsyncSession):
    """The `abs()` fix must not stop dropping genuine sub-cent dust."""
    acc = Account(name="Wallet", type="crypto_wallet", currency="USD",
                  initial_balance=Decimal("0"), is_active=True,
                  include_in_total=True, created_at=_utcnow(), updated_at=_utcnow())
    db.add(acc)
    await db.flush()
    asset = Asset(symbol="DUSTCOIN", name="DustCoin", asset_class="crypto", currency="EUR",
                  created_at=_utcnow(), updated_at=_utcnow())
    db.add(asset)
    await db.flush()
    db.add_all([
        AssetHolding(account_id=acc.id, asset_id=asset.id, quantity=Decimal("1"),
                     chain="ethereum", is_active=True, created_at=_utcnow(), updated_at=_utcnow()),
        MarketPrice(asset_id=asset.id, quoted_at=_utcnow(), price=Decimal("0.01"),
                    currency="EUR", source="test"),
    ])
    await db.commit()

    composition = await compute_composition(db, "EUR")
    by_label = {e["label"]: e for e in composition.entries}
    assert "DUSTCOIN" not in by_label
    assert composition.dust_excluded_count == 1


# ─── F15: uncategorized rows merged income + expense under 'expense' ─────


async def test_f15_uncategorized_income_and_expense_are_not_merged(
    client: AsyncClient, db: AsyncSession,
):
    """Regression F15: `/cashflow/by-category` grouped only by
    `t.category_id`, and the uncategorized (NULL) group defaulted `kind` to
    `'expense'` regardless of the underlying transaction type — so an
    uncategorized 100 income row and an uncategorized 20 expense row merged
    into a single row reported as `expense=120`. Fix: uncategorized rows
    fall back to `t.type` for kind and group separately per type.
    """
    # `cashflow.py` reads a module-level, process-cached `settings.base_currency`
    # (set at first import, not re-read from env per test), so match it here
    # rather than hardcoding "EUR" — otherwise these rows look like a foreign
    # currency with no FX data and get excluded (mirrors the pattern already
    # used in test_cashflow_by_category_fx.py).
    from app.core.config import get_settings
    cur = get_settings().base_currency

    acc = Account(name="Mixed", type="bank", currency=cur, initial_balance=Decimal("0"),
                  is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    db.add(acc)
    await db.flush()
    db.add_all([
        Transaction(account_id=acc.id, category_id=None, occurred_at="2026-09-10T00:00:00Z",
                    amount=Decimal("100"), currency=cur, type="income", source="manual",
                    is_pending=False, created_at=_utcnow(), updated_at=_utcnow()),
        Transaction(account_id=acc.id, category_id=None, occurred_at="2026-09-11T00:00:00Z",
                    amount=Decimal("20"), currency=cur, type="expense", source="manual",
                    is_pending=False, created_at=_utcnow(), updated_at=_utcnow()),
    ])
    await db.commit()

    resp = await client.get("/api/v1/cashflow/by-category",
                            params={"period": "2026-09"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    groups = {row["kind"]: Decimal(row["total"]) for row in resp.json()["data"]}
    assert groups == {"income": Decimal("100"), "expense": Decimal("20")}, (
        f"uncategorized breakdown={groups}"
    )


async def test_f15_adjustment_rows_excluded_from_by_category(
    client: AsyncClient, db: AsyncSession,
):
    """`adjustment` rows aren't income/expense/transfer activity and must
    not appear in the by-category breakdown."""
    from app.core.config import get_settings
    cur = get_settings().base_currency

    acc = Account(name="Adj", type="bank", currency=cur, initial_balance=Decimal("0"),
                  is_active=True, created_at=_utcnow(), updated_at=_utcnow())
    db.add(acc)
    await db.flush()
    db.add(Transaction(account_id=acc.id, category_id=None, occurred_at="2026-09-12T00:00:00Z",
                       amount=Decimal("50"), currency=cur, type="adjustment", source="manual",
                       is_pending=False, created_at=_utcnow(), updated_at=_utcnow()))
    await db.commit()

    resp = await client.get("/api/v1/cashflow/by-category",
                            params={"period": "2026-09"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    kinds = {row["kind"] for row in resp.json()["data"]}
    assert "adjustment" not in kinds, resp.json()["data"]
