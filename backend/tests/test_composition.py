"""Tests for portfolio composition (the by-構成 distribution view).

Rules under test:
- cash grouped by currency; snapshot accounts' ledger excluded from cash;
- stablecoins (USDT/USDC/…) merged into one bucket;
- same coin summed across accounts (BTC on two exchanges → one BTC);
- dust < €0.1 dropped;
- investment in [€0.1, €20) folded into a per-category 小额 bucket;
- everything priced in EUR (base) so no FX is needed.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", "composition" * 4)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base  # noqa: E402
from app.models import Account, Asset, AssetHolding, MarketPrice  # noqa: E402
from app.services.valuation.composition import compute_composition  # noqa: E402

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def _now() -> str:
    return "2026-06-29T00:00:00Z"


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from app.main import _BALANCE_VIEW_SQL
        await conn.execute(text(_BALANCE_VIEW_SQL))

    async with _Session() as db:
        bank = Account(name="Bank", type="bank", currency="EUR",
                       initial_balance=Decimal("1000"), is_active=True,
                       include_in_total=True, created_at=_now(), updated_at=_now())
        ex1 = Account(name="Ex1", type="exchange", currency="USDT",
                      initial_balance=Decimal("0"), is_active=True,
                      include_in_total=True, created_at=_now(), updated_at=_now())
        ex2 = Account(name="Ex2", type="exchange", currency="USDT",
                      initial_balance=Decimal("0"), is_active=True,
                      include_in_total=True, created_at=_now(), updated_at=_now())
        db.add_all([bank, ex1, ex2])
        await db.flush()

        def asset(symbol, cls):
            a = Asset(symbol=symbol, name=symbol, asset_class=cls, currency="EUR",
                      created_at=_now(), updated_at=_now())
            db.add(a)
            return a

        usdt, usdc = asset("USDT", "crypto"), asset("USDC", "crypto")
        btc, small, dust = asset("BTC", "crypto"), asset("SMALLC", "crypto"), asset("DUST", "crypto")
        aapl = asset("AAPL", "us_stock")
        # Broker-synced fund whose symbol is an ISIN but has a friendly name.
        isin = Asset(symbol="IE00B4L5Y983", name="Core MSCI World", asset_class="fund",
                     currency="EUR", created_at=_now(), updated_at=_now())
        db.add(isin)
        await db.flush()

        def price(a, p):
            db.add(MarketPrice(asset_id=a.id, price=Decimal(p), currency="EUR",
                               source="test", quoted_at=_now()))

        for a, p in [(usdt, "1"), (usdc, "1"), (btc, "50000"), (small, "5"),
                     (dust, "0.05"), (aapl, "100"), (isin, "200")]:
            price(a, p)

        def hold(acc, a, qty):
            db.add(AssetHolding(account_id=acc.id, asset_id=a.id, quantity=Decimal(qty),
                                chain="", is_active=True, created_at=_now(), updated_at=_now()))

        hold(ex1, usdt, "500")     # stable
        hold(ex1, usdc, "300")     # stable (merged → 800)
        hold(ex1, btc, "0.01")     # €500 in ex1
        hold(ex2, btc, "0.006")    # €300 in ex2 → BTC summed = €800
        hold(ex1, small, "1")      # €5 → 小额加密货币
        hold(ex1, dust, "1")       # €0.05 → dropped
        hold(ex1, aapl, "1")       # €100 → shown individually
        hold(ex1, isin, "1")       # €200 → label uses friendly name, not ISIN
        await db.commit()
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_composition_rules():
    async with _Session() as db:
        r = await compute_composition(db, "EUR")
    by_label = {e["label"]: e for e in r.entries}

    # Stablecoins merged into one bucket (USDT 500 + USDC 300 = 800).
    assert Decimal(by_label["USD 稳定币"]["value"]) == Decimal("800")
    assert by_label["USD 稳定币"]["count"] == 2
    # Cash grouped by currency (bank initial_balance).
    assert Decimal(by_label["EUR 现金"]["value"]) == Decimal("1000")
    # Same coin summed across accounts: BTC 0.01@ex1 + 0.006@ex2 = 0.016 × 50000.
    assert Decimal(by_label["BTC"]["value"]) == Decimal("800")
    assert by_label["BTC"]["count"] == 2
    # Small (€5) folded into the per-category bucket; AAPL (€100) shown alone.
    assert Decimal(by_label["小额加密货币"]["value"]) == Decimal("5")
    assert Decimal(by_label["AAPL"]["value"]) == Decimal("100")
    # Dust (€0.05) dropped; not present.
    assert "DUST" not in by_label
    assert r.dust_excluded_count == 1
    # ISIN-symbol asset shows its friendly name, not the raw ISIN.
    assert Decimal(by_label["Core MSCI World"]["value"]) == Decimal("200")
    assert "IE00B4L5Y983" not in by_label
    # Total reconciles (1000 + 800 + 800 + 5 + 100 + 200 = 2905; dust excluded).
    assert Decimal(r.total) == Decimal("2905")
