"""Brokerage sync (IBKR Flex Web Service).

- The 2-step download goes through ``httpx.MockTransport`` — no network.
  The handler routes by URL path (SendRequest vs GetStatement) and can
  return a "still generating" status first to exercise the retry loop.
- The XML parse → ``BrokerPosition`` mapping is verified against a real
  Flex statement fixture (positions + asset categories + currencies).
- The upsert layer is a pure DB test: fresh insert, price write, and the
  sold-position reset semantics.
- The orchestrator brokerage branch is tested with a stubbed provider.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")
# 32-byte hex so encrypt_str/decrypt_str work for the BrokerConnection token.
os.environ.setdefault(
    "FINANCE_BANK_ENCRYPTION_KEY",
    "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
)

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Asset,
    AssetHolding,
    BrokerConnection,
    MarketPrice,
)
from app.services.bank_sync.crypto import encrypt_str  # noqa: E402
from app.services.broker_sync import (  # noqa: E402
    BrokerPosition,
    BrokerSyncError,
    dispatch,
    map_asset_class,
)
from app.services.broker_sync.ibkr import IBKRFlexProvider  # noqa: E402
from app.services.broker_sync.upsert import apply_broker_snapshot  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "broker_sync"


def _fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def _noop_sleep(_seconds: float) -> None:
    return None


# ─── Test database ───────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(
    _engine, expire_on_commit=False, class_=AsyncSession
)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _TestingSessionLocal() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_brokerage_account(db: AsyncSession, name: str) -> Account:
    acc = Account(
        name=name,
        type="brokerage",
        currency="CNY",
        initial_balance=Decimal("0"),
        is_active=True,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(acc)
    await db.commit()
    return acc


# ─── asset-class mapping ─────────────────────────────────────────────────────


class TestAssetClassMapping:
    def test_stock_by_currency(self):
        assert map_asset_class("STK", "USD") == "us_stock"
        assert map_asset_class("STK", "EUR") == "eu_stock"
        assert map_asset_class("STK", "CNY") == "a_share"

    def test_fund_and_bond(self):
        assert map_asset_class("FUND", "USD") == "fund"
        assert map_asset_class("ETF", "USD") == "fund"
        assert map_asset_class("BOND", "USD") == "bond"

    def test_unknown_falls_back_to_other(self):
        assert map_asset_class("WAR", "USD") == "other"
        assert map_asset_class("", "") == "other"

    def test_stock_unknown_currency_defaults_us(self):
        assert map_asset_class("STK", "JPY") == "us_stock"


# ─── IBKR Flex provider (2-step download via MockTransport) ──────────────────


def _make_handler(get_responses: list[httpx.Response]):
    """Route SendRequest → success; GetStatement → pop next from a queue."""
    queue = list(get_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("SendRequest"):
            return httpx.Response(200, content=_fx("send_request.xml"))
        if path.endswith("GetStatement"):
            return queue.pop(0)
        return httpx.Response(404)

    return handler


class TestIBKRFlexProvider:
    async def test_happy_path_parses_positions(self):
        handler = _make_handler([httpx.Response(200, content=_fx("ibkr_flex.xml"))])
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = IBKRFlexProvider(
            token="tok", query_id="q1", client=client, sleep=_noop_sleep
        )
        positions = await provider.fetch_positions()
        await client.aclose()

        # 3 securities + 2 cash currencies (EUR, USD); BASE_SUMMARY skipped.
        assert len(positions) == 5
        by_symbol = {p.symbol: p for p in positions}
        assert by_symbol["AAPL"].quantity == Decimal("100")
        assert by_symbol["AAPL"].mark_price == Decimal("195.50")
        assert by_symbol["AAPL"].currency == "USD"
        assert by_symbol["AAPL"].conid == "265598"
        assert by_symbol["AAPL"].avg_cost == Decimal("150.00")
        assert by_symbol["SAP"].currency == "EUR"
        assert by_symbol["VWRA"].asset_category == "FUND"
        # cash positions: priced 1:1, asset_class=cash, per currency
        assert by_symbol["EUR"].asset_class == "cash"
        assert by_symbol["EUR"].quantity == Decimal("1000.50")
        assert by_symbol["EUR"].mark_price == Decimal("1")
        assert by_symbol["USD"].asset_class == "cash"
        assert by_symbol["USD"].quantity == Decimal("500.25")
        assert "BASE_SUMMARY" not in by_symbol  # aggregate row skipped

    async def test_retries_while_generating(self):
        # First GetStatement says "1019 generating", second returns the data.
        handler = _make_handler([
            httpx.Response(200, content=_fx("generating.xml")),
            httpx.Response(200, content=_fx("ibkr_flex.xml")),
        ])
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = IBKRFlexProvider(
            token="tok", query_id="q1", client=client,
            sleep=_noop_sleep, poll_interval_sec=0.0,
        )
        positions = await provider.fetch_positions()
        await client.aclose()
        assert len(positions) == 5  # 3 securities + 2 cash currencies

    async def test_bad_token_raises_broker_sync_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_fx("bad_token.xml"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = IBKRFlexProvider(
            token="bad", query_id="q1", client=client, sleep=_noop_sleep
        )
        with pytest.raises(BrokerSyncError) as exc:
            await provider.fetch_positions()
        await client.aclose()
        assert "1015" in str(exc.value)

    async def test_generation_timeout(self):
        # Always "generating" → exhaust max_tries → BrokerSyncError.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("SendRequest"):
                return httpx.Response(200, content=_fx("send_request.xml"))
            return httpx.Response(200, content=_fx("generating.xml"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = IBKRFlexProvider(
            token="tok", query_id="q1", client=client,
            sleep=_noop_sleep, max_tries=3, poll_interval_sec=0.0,
        )
        with pytest.raises(BrokerSyncError):
            await provider.fetch_positions()
        await client.aclose()

    def test_dispatch_unknown_provider(self):
        with pytest.raises(ValueError):
            dispatch("schwab", token="t", query_id="q")


# ─── upsert ──────────────────────────────────────────────────────────────────


def _positions() -> list[BrokerPosition]:
    return [
        BrokerPosition(
            symbol="AAPL", conid="265598", asset_category="STK", currency="USD",
            quantity=Decimal("100"), mark_price=Decimal("195.50"),
            avg_cost=Decimal("150.00"), description="APPLE INC",
        ),
        BrokerPosition(
            symbol="SAP", conid="111111", asset_category="STK", currency="EUR",
            quantity=Decimal("50"), mark_price=Decimal("180.00"),
            avg_cost=Decimal("160.00"), description="SAP SE",
        ),
    ]


class TestBrokerUpsert:
    async def test_fresh_insert_creates_assets_holdings_prices(self, db: AsyncSession):
        acc = await _make_brokerage_account(db, "IBKR-Fresh")
        n = await apply_broker_snapshot(db, acc.id, _positions())
        await db.commit()

        assert n == 2
        holdings = (
            await db.execute(
                select(AssetHolding).where(AssetHolding.account_id == acc.id)
            )
        ).scalars().all()
        assert len(holdings) == 2
        for h in holdings:
            await db.refresh(h, ["asset"])
        by_symbol = {h.asset.symbol: h for h in holdings}
        assert by_symbol["AAPL"].quantity == Decimal("100")
        assert by_symbol["AAPL"].chain == ""
        assert by_symbol["AAPL"].asset.asset_class == "us_stock"
        assert by_symbol["AAPL"].asset.data_source_id == "265598"
        assert by_symbol["SAP"].asset.asset_class == "eu_stock"

        # A market price was written per position, in its native currency.
        aapl_price = (
            await db.execute(
                select(MarketPrice).where(MarketPrice.asset_id == by_symbol["AAPL"].asset_id)
            )
        ).scalar_one()
        assert aapl_price.price == Decimal("195.50")
        assert aapl_price.currency == "USD"
        assert aapl_price.source == "ibkr"

    async def test_resync_zeroes_sold_positions(self, db: AsyncSession):
        acc = await _make_brokerage_account(db, "IBKR-Resync")
        await apply_broker_snapshot(db, acc.id, _positions())
        await db.commit()

        # Second sync: AAPL sold, only SAP remains (qty changed).
        await apply_broker_snapshot(db, acc.id, [
            BrokerPosition(
                symbol="SAP", conid="111111", asset_category="STK", currency="EUR",
                quantity=Decimal("40"), mark_price=Decimal("181.00"),
                avg_cost=Decimal("160.00"), description="SAP SE",
            ),
        ])
        await db.commit()

        holdings = (
            await db.execute(
                select(AssetHolding).where(AssetHolding.account_id == acc.id)
            )
        ).scalars().all()
        for h in holdings:
            await db.refresh(h, ["asset"])
        by_symbol = {h.asset.symbol: h for h in holdings}
        assert by_symbol["SAP"].quantity == Decimal("40")
        assert by_symbol["SAP"].is_active is True
        assert by_symbol["AAPL"].quantity == Decimal("0")
        assert by_symbol["AAPL"].is_active is False

    async def test_reuses_existing_asset_and_backfills_conid(self, db: AsyncSession):
        # Pre-existing manual asset with no data_source_id (unique symbol so it
        # doesn't collide with other tests sharing the module-scoped DB).
        manual = Asset(
            symbol="MSFT", name="Microsoft", asset_class="us_stock", currency="USD",
            chain="", contract="",
        )
        db.add(manual)
        await db.flush()
        acc = await _make_brokerage_account(db, "IBKR-Reuse")
        await apply_broker_snapshot(db, acc.id, [
            BrokerPosition(
                symbol="MSFT", conid="777777", asset_category="STK", currency="USD",
                quantity=Decimal("5"), mark_price=Decimal("400.00"),
                avg_cost=Decimal("300.00"), description="MICROSOFT CORP",
            ),
        ])
        await db.commit()

        assets = (
            await db.execute(select(Asset).where(Asset.symbol == "MSFT"))
        ).scalars().all()
        assert len(assets) == 1, "must reuse the manual MSFT row, not duplicate"
        assert assets[0].data_source_id == "777777"


# ─── orchestrator brokerage branch ───────────────────────────────────────────


class TestOrchestratorBrokerage:
    async def test_sync_account_brokerage(self, db: AsyncSession, monkeypatch):
        from app.services.wallet_sync import orchestrator

        acc = await _make_brokerage_account(db, "IBKR-Orch")
        db.add(BrokerConnection(
            account_id=acc.id,
            provider="ibkr",
            token_enc=encrypt_str("flex-token"),
            query_id="q1",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        await db.commit()

        class _StubProvider:
            provider_id = "ibkr"

            async def fetch_positions(self):
                return _positions()

        def _stub_dispatch(provider, *, token, query_id):
            assert token == "flex-token"
            assert query_id == "q1"
            return _StubProvider()

        monkeypatch.setattr(orchestrator, "_dispatch_broker", _stub_dispatch)

        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()

        assert summary.account_type == "brokerage"
        assert summary.total_synced == 2
        assert summary.total_errors == 0

        holdings = (
            await db.execute(
                select(AssetHolding).where(
                    AssetHolding.account_id == acc.id,
                    AssetHolding.is_active == True,  # noqa: E712
                )
            )
        ).scalars().all()
        assert len(holdings) == 2

    async def test_broker_error_captured_not_raised(self, db: AsyncSession, monkeypatch):
        from app.services.wallet_sync import orchestrator

        acc = await _make_brokerage_account(db, "IBKR-Err")
        db.add(BrokerConnection(
            account_id=acc.id,
            provider="ibkr",
            token_enc=encrypt_str("flex-token"),
            query_id="q1",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        await db.commit()

        def _boom_dispatch(provider, *, token, query_id):
            raise BrokerSyncError("IBKR Flex error 1015: Flex token is invalid")

        monkeypatch.setattr(orchestrator, "_dispatch_broker", _boom_dispatch)

        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()
        assert summary.total_errors == 1
        assert "1015" in summary.results[0].error


# ─── Trade Republic ──────────────────────────────────────────────────────────


# compactPortfolioByType shape (categories → positions, no live price).
_TR_PORTFOLIO = {
    "categories": [
        {
            "categoryType": "stocksAndETFs",
            "positions": [
                {"isin": "IE00B4L5Y983", "netSize": "2.41794", "averageBuyIn": "124.883",
                 "instrumentType": "fund", "name": "Core MSCI World USD (Acc)"},
                {"isin": "US0846707026", "netSize": "1.831242", "averageBuyIn": "437.8558",
                 "instrumentType": "stock", "name": "Berkshire Hathaway (B)"},
            ],
        }
    ]
}
# ticker price per ISIN (LSX).
_TR_TICKERS = {
    "IE00B4L5Y983": {"last": {"price": "123.925"}},
    "US0846707026": {"last": {"price": "432.45"}},
}


class _FakeTR:
    """Stand-in for pytr's TradeRepublicApi — no network. Drives the
    subscribe→_recv_subscription flow for compactPortfolioByType + ticker."""

    def __init__(self, cookies_path, *, portfolio_data=None, tickers=None, resume_ok=True):
        self._cookies_file = str(cookies_path)
        self._portfolio_data = portfolio_data or {}
        self._tickers = tickers or {}
        self._resume_ok = resume_ok
        self._subs: dict[str, dict] = {}
        self._n = 0

    # login (sync)
    def initiate_weblogin(self):
        return 30

    def complete_weblogin(self, code):
        return None

    def save_websession(self):
        from pathlib import Path

        Path(self._cookies_file).write_text("# Netscape cookie\nfake\tcookie\tdata\n")

    def resume_websession(self):
        return self._resume_ok

    # websocket (async)
    async def subscribe(self, payload):
        self._n += 1
        sid = str(self._n)
        self._subs[sid] = payload
        return sid

    async def _recv_subscription(self, sub_id):
        payload = self._subs.get(sub_id, {})
        if payload.get("type") == "compactPortfolioByType":
            return self._portfolio_data
        if payload.get("type") == "ticker":
            isin = (payload.get("id") or "").split(".")[0]
            return self._tickers.get(isin, {})
        return {}

    async def unsubscribe(self, sub_id):
        self._subs.pop(sub_id, None)

    async def close(self):
        pass


class TestTradeRepublicMapping:
    def test_instrument_type_to_asset_class(self):
        from app.services.broker_sync.traderepublic import map_instrument_type_to_asset_class as m

        assert m("stock", "US0846707026") == "us_stock"
        assert m("stock", "DE000BASF111") == "eu_stock"
        assert m("fund", "IE00B4L5Y983") == "fund"
        assert m("bond", "DE0001102580") == "bond"
        assert m("crypto", "XF000BTC0017") == "crypto"
        assert m("derivative", "DE000XYZ1234") == "other"

    def test_normalize_phone(self):
        from app.services.broker_sync.traderepublic import normalize_phone

        assert normalize_phone("+49 1512 3456789") == "+4915123456789"
        assert normalize_phone("0049-1512-3456789") == "+4915123456789"
        assert normalize_phone(" +4915123456789 ") == "+4915123456789"

    def test_flatten_and_build(self):
        from app.services.broker_sync.traderepublic import _flatten_positions, _build_position

        raws = _flatten_positions(_TR_PORTFOLIO)
        assert len(raws) == 2
        p = _build_position(raws[0], Decimal("123.925"))
        assert p.symbol == "IE00B4L5Y983"
        assert p.quantity == Decimal("2.41794")
        assert p.mark_price == Decimal("123.925")
        assert p.avg_cost == Decimal("124.883")
        assert p.currency == "EUR"
        assert p.asset_class == "fund"
        assert p.description == "Core MSCI World USD (Acc)"


class TestTradeRepublicProvider:
    async def test_fetch_positions_with_prices(self, monkeypatch):
        from app.services.broker_sync import traderepublic as trmod

        monkeypatch.setattr(
            trmod, "_new_api",
            lambda path, **kw: _FakeTR(path, portfolio_data=_TR_PORTFOLIO, tickers=_TR_TICKERS),
        )
        prov = trmod.TradeRepublicProvider(cookies_blob="cookies-here")
        positions = await prov.fetch_positions()
        assert len(positions) == 2
        by = {p.symbol: p for p in positions}
        assert by["US0846707026"].mark_price == Decimal("432.45")
        assert by["US0846707026"].asset_class == "us_stock"
        assert by["IE00B4L5Y983"].mark_price == Decimal("123.925")
        assert by["IE00B4L5Y983"].asset_class == "fund"

    async def test_missing_ticker_keeps_position(self, monkeypatch):
        from app.services.broker_sync import traderepublic as trmod

        # No tickers → price unknown, but holdings still returned.
        monkeypatch.setattr(
            trmod, "_new_api",
            lambda path, **kw: _FakeTR(path, portfolio_data=_TR_PORTFOLIO, tickers={}),
        )
        prov = trmod.TradeRepublicProvider(cookies_blob="c")
        positions = await prov.fetch_positions()
        assert len(positions) == 2
        assert all(p.mark_price is None for p in positions)

    async def test_expired_session_raises(self, monkeypatch):
        from app.services.broker_sync import BrokerSyncError
        from app.services.broker_sync import traderepublic as trmod

        monkeypatch.setattr(
            trmod, "_new_api",
            lambda path, **kw: _FakeTR(path, resume_ok=False),
        )
        prov = trmod.TradeRepublicProvider(cookies_blob="stale")
        with pytest.raises(BrokerSyncError):
            await prov.fetch_positions()

    def test_login_roundtrip_returns_cookies(self, monkeypatch):
        from app.services.broker_sync import traderepublic as trmod

        monkeypatch.setattr(trmod, "_new_api", lambda path, **kw: _FakeTR(path))
        tr, countdown = trmod.initiate_login("+4915123456789", "1234")
        assert countdown == 30
        cookies = trmod.complete_login(tr, "0000")
        assert "cookie" in cookies  # save_websession wrote the jar


class TestTradeRepublicSync:
    async def test_orchestrator_brokerage_tr(self, db: AsyncSession, monkeypatch):
        """End-to-end brokerage sync with a traderepublic connection row +
        stubbed provider — exercises the query_id=None path."""
        from app.services.wallet_sync import orchestrator

        acc = await _make_brokerage_account(db, "TR-Orch")
        db.add(BrokerConnection(
            account_id=acc.id,
            provider="traderepublic",
            token_enc=encrypt_str("cookie-blob"),
            query_id=None,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        await db.commit()

        class _StubProvider:
            provider_id = "traderepublic"

            async def fetch_positions(self):
                from app.services.broker_sync.traderepublic import (
                    _build_position, _dec, _flatten_positions,
                )
                return [
                    _build_position(r, _dec(_TR_TICKERS.get(r["isin"], {}).get("last", {}).get("price")))
                    for r in _flatten_positions(_TR_PORTFOLIO)
                ]

        def _stub_dispatch(provider, *, token, query_id):
            assert provider == "traderepublic"
            assert token == "cookie-blob"
            assert query_id is None
            return _StubProvider()

        monkeypatch.setattr(orchestrator, "_dispatch_broker", _stub_dispatch)
        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()

        assert summary.account_type == "brokerage"
        assert summary.total_synced == 2
        assert summary.total_errors == 0

        holdings = (
            await db.execute(
                select(AssetHolding).where(
                    AssetHolding.account_id == acc.id,
                    AssetHolding.is_active == True,  # noqa: E712
                )
            )
        ).scalars().all()
        assert len(holdings) == 2
