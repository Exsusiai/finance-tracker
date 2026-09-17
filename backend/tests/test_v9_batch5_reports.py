"""V9 review batch 5 — F19 (portfolio unrealized P&L mixed-currency) regression.

`GET /holdings/portfolio/summary` used to have no P&L field at all; the
frontend summed each holding's raw `unrealized_pnl` (denominated in that
holding's own quote currency) as if they were all the same currency — 100
EUR profit + 100 USD profit displayed as "200" instead of ~190. The fix adds
`unrealized_pnl_base` (converted per-holding via
`services/valuation/fx.convert_to_base`, then summed) and
`pnl_unconvertible_count` (holdings whose pnl currency has no FX path, so
they're excluded from the sum instead of silently corrupting it).

Also covers the /holdings list regression: it previously had no Account
join at all, so a soft-deleted account's holdings would linger as orphans
in the holdings list forever (even though the account itself disappears
from /accounts and the whole UI).
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_TEST_TOKEN = "v9batch5v9batch5v9batch5v9batch5v9batch5"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
# BASE_CURRENCY is process-wide and `setdefault` is a no-op once another
# test module (running earlier in the same pytest process) has already set
# it — most suites agree on the project-default CNY, so this file follows
# that convention rather than fighting for a different value.
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Asset, AssetHolding, FxRate, MarketPrice, _utcnow_str  # noqa: E402

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    poolclass=StaticPool,
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
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.pop(get_db, None) if prev is None else app.dependency_overrides.__setitem__(get_db, prev)


@pytest_asyncio.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _now() -> str:
    return _utcnow_str()


async def _account(name: str, currency: str = "CNY", include_in_total: bool = True,
                    deleted_at: str | None = None) -> Account:
    async with _Session() as s:
        a = Account(
            name=name, type="brokerage", currency=currency,
            initial_balance=Decimal("0"), is_active=True,
            include_in_total=include_in_total, deleted_at=deleted_at,
            created_at=_now(), updated_at=_now(),
        )
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _asset(symbol: str) -> Asset:
    async with _Session() as s:
        a = Asset(symbol=symbol, name=symbol, asset_class="us_stock", currency="USD",
                   created_at=_now(), updated_at=_now())
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _price(asset: Asset, value: str, currency: str) -> None:
    async with _Session() as s:
        s.add(MarketPrice(asset_id=asset.id, quoted_at=_now(), price=Decimal(value),
                           currency=currency, source="test"))
        await s.commit()


async def _holding(account: Account, asset: Asset, qty: str, avg_cost: str,
                    cost_currency: str, is_active: bool = True) -> AssetHolding:
    async with _Session() as s:
        h = AssetHolding(
            account_id=account.id, asset_id=asset.id, chain="",
            quantity=Decimal(qty), avg_cost=Decimal(avg_cost), cost_currency=cost_currency,
            is_active=is_active, created_at=_now(), updated_at=_now(),
        )
        s.add(h)
        await s.commit()
        await s.refresh(h)
        return h


async def _fx(base: str, quote: str, rate: str) -> None:
    async with _Session() as s:
        s.add(FxRate(base_currency=base, quote_currency=quote, quoted_at=_now(),
                      rate=Decimal(rate), source="test"))
        await s.commit()


@pytest.mark.asyncio
async def test_summary_pnl_sums_across_currencies_via_fx(client: AsyncClient):
    """CNY holding +100 profit and USD holding +100 profit (FX USD->CNY 0.9)
    must sum to 190 CNY, not a naive 200."""
    await _fx("USD", "CNY", "0.9")

    acc_eur = await _account("PNL-EUR-Acc", currency="CNY")
    acc_usd = await _account("PNL-USD-Acc", currency="USD")
    asset_eur = await _asset("PNL-EUR-STOCK")
    asset_usd = await _asset("PNL-USD-STOCK")

    # quantity 1, avg_cost 100 -> price 200 -> pnl = 100, in each holding's own currency
    await _price(asset_eur, "200", "CNY")
    await _price(asset_usd, "200", "USD")
    await _holding(acc_eur, asset_eur, "1", "100", "CNY")
    await _holding(acc_usd, asset_usd, "1", "100", "USD")

    resp = await client.get("/api/v1/holdings/portfolio/summary", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["base_currency"] == "CNY"
    assert Decimal(data["unrealized_pnl_base"]) == Decimal("190")
    assert data["pnl_unconvertible_count"] == 0


@pytest.mark.asyncio
async def test_summary_pnl_excludes_include_in_total_false_account(client: AsyncClient):
    """A holding in an include_in_total=False account must not contribute to
    unrealized_pnl_base (same scope as total_value)."""
    acc_in = await _account("PNL-Scope-In", currency="CNY", include_in_total=True)
    acc_out = await _account("PNL-Scope-Out", currency="CNY", include_in_total=False)
    asset_in = await _asset("PNL-SCOPE-IN")
    asset_out = await _asset("PNL-SCOPE-OUT")

    await _price(asset_in, "150", "CNY")   # avg_cost 100 -> pnl 50
    await _price(asset_out, "1150", "CNY")  # avg_cost 100 -> pnl 1050 (should be excluded)
    await _holding(acc_in, asset_in, "1", "100", "CNY")
    await _holding(acc_out, asset_out, "1", "100", "CNY")

    resp = await client.get("/api/v1/holdings/portfolio/summary", headers=AUTH)
    data = resp.json()["data"]
    # Baseline (from the previous test) already contributed 190 CNY; this
    # test only asserts the delta introduced by its own two holdings by
    # checking the *increment* is exactly 50, not 1100.
    # Since these tests share one in-memory DB (module-scoped), compare
    # against a freshly scoped assertion instead: pnl must not include 1050.
    assert Decimal(data["unrealized_pnl_base"]) < Decimal("1050")


@pytest.mark.asyncio
async def test_summary_pnl_unconvertible_counted_not_dropped_silently(client: AsyncClient):
    """A holding whose price currency has no FX path to base is excluded
    from the sum but counted in pnl_unconvertible_count."""
    acc = await _account("PNL-NoFX-Acc", currency="CNY")
    asset = await _asset("PNL-NOFX-STOCK")
    await _price(asset, "200", "ZWL")  # no FX rate exists for ZWL anywhere
    await _holding(acc, asset, "1", "100", "ZWL")

    resp = await client.get("/api/v1/holdings/portfolio/summary", headers=AUTH)
    data = resp.json()["data"]
    assert data["pnl_unconvertible_count"] >= 1


@pytest.mark.asyncio
async def test_holdings_list_excludes_soft_deleted_account(client: AsyncClient):
    """/holdings must not return holdings belonging to a soft-deleted
    account — that account is already gone from /accounts entirely."""
    acc = await _account("PNL-SoftDel-Acc", currency="CNY", deleted_at=_now())
    asset = await _asset("PNL-SOFTDEL-STOCK")
    await _price(asset, "100", "CNY")
    holding = await _holding(acc, asset, "1", "50", "CNY")

    resp = await client.get("/api/v1/holdings", headers=AUTH)
    assert resp.status_code == 200
    ids = [h["id"] for h in resp.json()["data"]]
    assert holding.id not in ids


@pytest.mark.asyncio
async def test_holdings_list_still_shows_include_in_total_false_and_inactive(client: AsyncClient):
    """/holdings keeps showing holdings from include_in_total=False accounts
    and is_active=False holdings — only the aggregated summary/breakdown
    excludes them, per the task spec."""
    acc = await _account("PNL-List-Visible-Acc", currency="CNY", include_in_total=False)
    asset = await _asset("PNL-LIST-VISIBLE-STOCK")
    await _price(asset, "100", "CNY")
    visible_excluded_scope = await _holding(acc, asset, "1", "50", "CNY")

    acc2 = await _account("PNL-List-Inactive-Acc", currency="CNY")
    asset2 = await _asset("PNL-LIST-INACTIVE-STOCK")
    await _price(asset2, "100", "CNY")
    inactive_holding = await _holding(acc2, asset2, "1", "50", "CNY", is_active=False)

    resp = await client.get("/api/v1/holdings", headers=AUTH)
    ids = [h["id"] for h in resp.json()["data"]]
    assert visible_excluded_scope.id in ids
    assert inactive_holding.id in ids
