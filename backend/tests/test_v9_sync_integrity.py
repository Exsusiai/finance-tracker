"""Regression tests for the V9 code-review sync-integrity bugs (F05/F06/F22).

Mirrors the two repro cases from ``code review/repro_v9.py`` at the
service layer (that file is read-only / not to be modified) plus new
coverage for F22, which had no repro.

- F05: syncing multiple addresses on the SAME chain used to call
  ``apply_balance_snapshot()`` once per address — each call REPLACES the
  whole (account, chain) snapshot, so the second address clobbered the
  first instead of aggregating.
- F06: a partial failure inside an exchange/broker fetch (one Bitget
  sub-endpoint, or Trade Republic's cash feed) used to be swallowed and
  the *remaining* data returned as if it were the complete balance — the
  caller then persisted that partial snapshot, zeroing out whatever
  wasn't present in the incomplete round.
- F22: the generic crypto price refresh (scheduler job + "refresh market
  data" button) explicitly excluded wallet/exchange-synced holdings, and
  the stock refresh fed IBKR conids / Trade Republic ISINs to yfinance.
"""

from __future__ import annotations

import os
from decimal import Decimal

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
    ChainAddress,
    ExchangeConnection,
    MarketPrice,
)
from app.services.bank_sync.crypto import encrypt_str  # noqa: E402
from app.services.broker_sync import BrokerPosition  # noqa: E402
from app.services.crypto_sync import BalanceItem  # noqa: E402

# ─── Test DB — function-scoped so tests across account types never bleed ────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session
    await engine.dispose()


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_wallet(db: AsyncSession, name: str = "Wallet") -> Account:
    acc = Account(
        name=name, type="crypto_wallet", currency="USDT",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    await db.commit()
    return acc


async def _make_exchange_account(db: AsyncSession, name: str = "Exchange") -> Account:
    acc = Account(
        name=name, type="exchange", currency="USDT",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    await db.commit()
    return acc


async def _make_brokerage_account(db: AsyncSession, name: str = "Broker") -> Account:
    acc = Account(
        name=name, type="brokerage", currency="EUR",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    await db.commit()
    return acc


@pytest_asyncio.fixture(autouse=True)
async def _stub_coingecko(monkeypatch):
    """Keep this module offline — the post-sync price refresh must never
    hit the live CoinGecko API just because a test exercises a sync path
    that happens to succeed."""
    from app.services.wallet_sync import orchestrator as o

    async def _noop_native(symbols, *, http=None):
        return {}

    async def _noop_token(chain, contracts, *, http=None):
        return {}

    monkeypatch.setattr(o, "fetch_native_prices", _noop_native)
    monkeypatch.setattr(o, "fetch_token_prices", _noop_token)


# ─── F05: same-chain addresses must aggregate, not clobber ─────────────────


class TestF05ChainAddressAggregation:
    async def test_same_chain_addresses_are_aggregated(self, db: AsyncSession, monkeypatch):
        """Repro: 1 ETH address + 2 ETH address on the same chain must sum
        to 3 ETH, not clobber down to whichever address synced last."""
        from app.services.wallet_sync import orchestrator as o

        class Provider:
            async def fetch_balances(self, address):
                return [BalanceItem(symbol="ETH", contract=None, quantity=Decimal(address), decimals=18)]

        monkeypatch.setattr(o, "_dispatch_chain", lambda *args: Provider())

        acc = await _make_wallet(db)
        db.add_all([
            ChainAddress(account_id=acc.id, chain="ethereum", address="1",
                         created_at=_utcnow(), updated_at=_utcnow()),
            ChainAddress(account_id=acc.id, chain="ethereum", address="2",
                         created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await db.commit()

        summary = await o.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()

        assert summary.total_errors == 0
        # ONE replacement for the chain, not one per address.
        assert len(summary.results) == 1
        holding = (
            await db.execute(select(AssetHolding).where(AssetHolding.account_id == acc.id))
        ).scalar_one()
        assert holding.quantity == Decimal("3"), f"1 ETH + 2 ETH became {holding.quantity} ETH"

    async def test_one_address_failure_preserves_previous_chain_snapshot(
        self, db: AsyncSession, monkeypatch
    ):
        """If any address on a chain fails to fetch, the chain's PREVIOUS
        snapshot must survive untouched — not get replaced by whatever the
        other addresses alone reported this round."""
        from app.services.wallet_sync import orchestrator as o
        from app.services.wallet_sync.upsert import apply_balance_snapshot

        class Provider:
            async def fetch_balances(self, address):
                if address == "bad":
                    raise RuntimeError("rate limit")
                return [BalanceItem(symbol="ETH", contract=None, quantity=Decimal("1"), decimals=18)]

        monkeypatch.setattr(o, "_dispatch_chain", lambda *args: Provider())

        acc = await _make_wallet(db)
        db.add_all([
            ChainAddress(account_id=acc.id, chain="ethereum", address="ok",
                         created_at=_utcnow(), updated_at=_utcnow()),
            ChainAddress(account_id=acc.id, chain="ethereum", address="bad",
                         created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [BalanceItem(symbol="ETH", contract=None, quantity=Decimal("5"), decimals=18)],
        )
        await db.commit()

        summary = await o.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()

        assert summary.total_errors == 1
        holding = (
            await db.execute(select(AssetHolding).where(AssetHolding.account_id == acc.id))
        ).scalar_one()
        assert holding.quantity == Decimal("5"), (
            f"partial address failure replaced the previous snapshot: {holding.quantity}"
        )


# ─── F06: partial exchange / broker responses must not zero prior data ─────


class TestF06BitgetPartialFailurePreservesHoldings:
    async def test_spot_failure_preserves_previous_holdings(self, db: AsyncSession, monkeypatch):
        """Repro: spot 503s, futures succeed. Previously the futures-only
        total was persisted as "complete", zeroing the pre-existing BTC
        holding that only spot would have reported. Now the whole fetch
        must fail and the sync must record an error without touching
        asset_holdings."""
        from app.services.wallet_sync import orchestrator as o
        from app.services.wallet_sync.upsert import apply_balance_snapshot
        from app.services.exchange_sync.bitget import BitgetProvider

        def respond(request):
            if request.url.path == "/api/v2/spot/account/assets":
                return httpx.Response(503)
            rows = (
                [{"marginCoin": "USDT", "available": "10", "locked": "0"}]
                if request.url.params.get("productType") == "USDT-FUTURES"
                else []
            )
            return httpx.Response(200, json={"code": "00000", "data": rows})

        acc = await _make_exchange_account(db)
        db.add(ExchangeConnection(
            account_id=acc.id, exchange="bitget",
            api_key_enc=encrypt_str("k"), api_secret_enc=encrypt_str("s"),
            api_passphrase_enc=encrypt_str("pp"),
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await apply_balance_snapshot(
            db, acc.id, "",
            [BalanceItem(symbol="BTC", contract=None, quantity=Decimal("1"), decimals=8)],
        )
        await db.commit()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://mock.invalid"
        ) as http:
            monkeypatch.setattr(o, "_dispatch_exchange", lambda *args: BitgetProvider(http=http))
            summary = await o.sync_account(db, acc.id, alchemy_api_key=None)
            await db.commit()

        assert summary.total_errors == 1
        btc = (
            await db.execute(
                select(AssetHolding.quantity).join(Asset).where(Asset.symbol == "BTC")
            )
        ).scalar_one()
        assert btc == Decimal("1"), f"503 on spot removed BTC; quantity={btc}"


class TestF06TradeRepublicCashFailurePreservesSnapshot:
    async def test_cash_feed_failure_preserves_previous_broker_snapshot(
        self, db: AsyncSession, monkeypatch
    ):
        """A Trade Republic cash-feed failure must not read as "zero cash"
        — apply_broker_snapshot() would zero out any position type absent
        from a "complete" round, wiping the real cash balance. The whole
        sync must fail instead, preserving the previous stock + cash
        snapshot."""
        from app.services.wallet_sync import orchestrator as o
        from app.services.broker_sync import traderepublic as trmod
        from app.services.broker_sync.upsert import apply_broker_snapshot

        acc = await _make_brokerage_account(db)
        db.add(BrokerConnection(
            account_id=acc.id, provider="traderepublic",
            token_enc=encrypt_str("cookie-blob"), query_id=None,
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await apply_broker_snapshot(
            db, acc.id,
            [
                BrokerPosition(
                    symbol="US0846707026", conid="US0846707026", asset_category="stock",
                    currency="EUR", quantity=Decimal("1"), mark_price=Decimal("400"),
                    avg_cost=Decimal("300"), description="Berkshire", asset_class="us_stock",
                ),
                BrokerPosition(
                    symbol="EUR", conid=None, asset_category="cash", currency="EUR",
                    quantity=Decimal("500"), mark_price=Decimal("1"), avg_cost=None,
                    description="EUR 现金", asset_class="cash",
                ),
            ],
            source="traderepublic",
        )
        await db.commit()

        class _FailingCashTR:
            """Stand-in for pytr's TradeRepublicApi: portfolio subscribes
            fine, but the cash-feed subscription blows up."""

            def __init__(self, cookies_path, **kw):
                self._cookies_file = str(cookies_path)

            def resume_websession(self):
                return True

            async def subscribe(self, payload):
                if payload.get("type") == "cash":
                    raise RuntimeError("websocket closed")
                return "1"

            async def _recv_subscription(self, sub_id):
                return {"categories": []}  # no stock positions this round

            async def unsubscribe(self, sub_id):
                pass

            async def close(self):
                pass

        monkeypatch.setattr(trmod, "_new_api", lambda path, **kw: _FailingCashTR(path))
        monkeypatch.setattr(
            o, "_dispatch_broker",
            lambda provider, *, token, query_id: trmod.TradeRepublicProvider(cookies_blob=token),
        )

        summary = await o.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()

        assert summary.total_errors == 1
        holdings = (
            await db.execute(
                select(AssetHolding).where(
                    AssetHolding.account_id == acc.id,
                    AssetHolding.is_active == True,  # noqa: E712
                )
            )
        ).scalars().all()
        assert len(holdings) == 2, (
            f"cash-feed failure wiped the previous broker snapshot: {len(holdings)} holdings left"
        )


# ─── F22: generic price refresh must cover wallet-synced crypto holdings ───
# and must not feed IBKR conids / Trade Republic ISINs to yfinance. ────────


class TestF22CryptoRefreshCoversWalletSyncedHoldings:
    async def test_refresh_crypto_prices_prices_native_onchain_holding(
        self, db: AsyncSession, monkeypatch
    ):
        """Before F22, refresh_crypto_prices() (scheduler job + "refresh
        market data" button) explicitly excluded onchain/native holdings —
        only an account *sync* priced them. A wallet-synced ETH holding
        must now get a fresh price from the generic refresh too."""
        from app.services.market_data import engine as market_engine
        from app.services.wallet_sync import orchestrator as o

        acc = await _make_wallet(db, "GlobalRefresh")
        asset = Asset(
            symbol="ETH", name="Ethereum", asset_class="crypto", currency="USDT",
            chain="", contract="", data_source="native", data_source_id=None,
        )
        db.add(asset)
        await db.flush()
        db.add(AssetHolding(
            account_id=acc.id, asset_id=asset.id, chain="", quantity=Decimal("2"),
            is_active=True, created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await db.commit()

        async def fake_native_prices(symbols, *, http=None):
            return {"ETH": Decimal("3000")}

        monkeypatch.setattr(o, "fetch_native_prices", fake_native_prices)

        result = await market_engine.refresh_crypto_prices(db)
        await db.commit()

        assert not result["errors"], result["errors"]
        assert result["prices_updated"] == 1
        price = (
            await db.execute(select(MarketPrice).where(MarketPrice.asset_id == asset.id))
        ).scalar_one()
        assert price.price == Decimal("3000")
        assert price.source == "coingecko"

    async def test_brokerage_stock_holding_is_not_swept_into_crypto_refresh(
        self, db: AsyncSession, monkeypatch
    ):
        """The global refresh must stay scoped to asset_class='crypto' — a
        brokerage stock ticker that collides with a crypto symbol (e.g. the
        US stock "UNI") must not get mispriced via CoinGecko."""
        from app.services.market_data import engine as market_engine
        from app.services.wallet_sync import orchestrator as o

        broker_acc = await _make_brokerage_account(db, "Broker")
        stock = Asset(
            symbol="UNI", name="Uniti Group", asset_class="us_stock", currency="USD",
            chain="", contract="", data_source="ibkr", data_source_id="12345",
        )
        db.add(stock)
        await db.flush()
        db.add(AssetHolding(
            account_id=broker_acc.id, asset_id=stock.id, chain="", quantity=Decimal("10"),
            is_active=True, created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await db.commit()

        async def fake_native_prices(symbols, *, http=None):
            # Would happily "price" UNI as the Uniswap token if it were
            # ever handed to this function.
            return {"UNI": Decimal("7")}

        monkeypatch.setattr(o, "fetch_native_prices", fake_native_prices)

        result = await market_engine.refresh_crypto_prices(db)
        await db.commit()

        assert result["prices_updated"] == 0
        assert (
            await db.execute(select(MarketPrice).where(MarketPrice.asset_id == stock.id))
        ).scalar_one_or_none() is None


class TestF22StockRefreshSkipsBrokerConidIsin:
    async def test_ibkr_and_traderepublic_identifiers_are_not_sent_to_yfinance(
        self, db: AsyncSession, monkeypatch
    ):
        from app.services.market_data import engine as market_engine

        yahoo = Asset(
            symbol="AAPL", name="Apple", asset_class="us_stock", currency="USD",
            chain="", contract="", data_source=None, data_source_id="AAPL",
        )
        ibkr = Asset(
            symbol="AAPL2", name="Apple (IBKR)", asset_class="us_stock", currency="USD",
            chain="", contract="", data_source="ibkr", data_source_id="265598",
        )
        tr = Asset(
            symbol="BRKB", name="Berkshire (TR)", asset_class="us_stock", currency="EUR",
            chain="", contract="", data_source="traderepublic", data_source_id="US0846707026",
        )
        db.add_all([yahoo, ibkr, tr])
        await db.commit()

        queried: list[str] = []

        async def fake_fetch(asset: Asset):
            queried.append(asset.data_source_id)
            return {
                "quoted_at": _utcnow(), "price": Decimal("100"),
                "currency": asset.currency, "source": "yfinance",
            }

        monkeypatch.setattr(market_engine, "_fetch_stock_price", fake_fetch)

        result = await market_engine.refresh_stock_prices(db)
        await db.commit()

        assert queried == ["AAPL"], (
            f"ibkr/traderepublic conid/ISIN leaked into the yfinance lookup: {queried}"
        )
        assert result["skipped"] == 2
        assert result["prices_updated"] == 1
