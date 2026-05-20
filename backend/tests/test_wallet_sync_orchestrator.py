"""P1-4 A4.2: orchestrator that wires providers → upsert per account.

Providers are stubbed with ``FakeProvider`` so this layer never touches
the network. We exercise:

- crypto_wallet flow: multiple addresses across chains aggregate
  correctly, last_synced_at updates per row, partial failure on one
  chain doesn't abort the others.
- exchange flow: encrypted creds round-trip through bank_sync/crypto.py
  then drive the exchange provider.
- error capture: SyncSummary carries per-source error strings.
"""

from __future__ import annotations

import os
import secrets
from decimal import Decimal

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
# Set encryption key BEFORE importing crypto helpers (they read env at call).
os.environ.setdefault("FINANCE_BANK_ENCRYPTION_KEY", secrets.token_hex(32))

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AssetHolding,
    ChainAddress,
    ExchangeConnection,
)
from app.services.bank_sync.crypto import encrypt_str  # noqa: E402
from app.services.crypto_sync import BalanceItem  # noqa: E402
from app.services.wallet_sync import orchestrator  # noqa: E402


# ─── Test DB ────────────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    async with _Session() as session:
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


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


async def _make_exchange_account(db: AsyncSession, name: str = "Binance") -> Account:
    acc = Account(
        name=name, type="exchange", currency="USDT",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow(), updated_at=_utcnow(),
    )
    db.add(acc)
    await db.commit()
    return acc


# ─── Fake providers ────────────────────────────────────────────────────────


class FakeChainProvider:
    def __init__(self, chain: str, items: list[BalanceItem] | None = None,
                 raises: Exception | None = None):
        self.chain_id = chain
        self._items = items or []
        self._raises = raises

    async def fetch_balances(self, address: str) -> list[BalanceItem]:
        if self._raises is not None:
            raise self._raises
        return list(self._items)


class FakeExchangeProvider:
    def __init__(self, exchange: str, items: list[BalanceItem] | None = None,
                 raises: Exception | None = None):
        self.exchange_id = exchange
        self._items = items or []
        self._raises = raises

    async def fetch_balances(self, api_key: str, api_secret: str,
                             passphrase: str | None = None) -> list[BalanceItem]:
        if self._raises is not None:
            raise self._raises
        return list(self._items)


@pytest_asyncio.fixture(autouse=True)
async def _stub_coingecko(monkeypatch):
    """Prevent any test in this module from accidentally hitting the live
    CoinGecko API via the orchestrator's price-refresh step. Individual
    tests can still re-monkeypatch when they want a specific price."""
    async def _noop_native(symbol, *, http=None):
        return None
    async def _noop_native_batched(symbols, *, http=None):
        return {}
    async def _noop_token(chain, contracts, *, http=None):
        return {}
    monkeypatch.setattr(orchestrator, "fetch_native_price", _noop_native)
    monkeypatch.setattr(orchestrator, "fetch_native_prices", _noop_native_batched)
    monkeypatch.setattr(orchestrator, "fetch_token_prices", _noop_token)


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestCryptoWalletFlow:
    async def test_multi_chain_aggregation(self, db: AsyncSession, monkeypatch):
        acc = await _make_wallet(db, "Multi")
        db.add_all([
            ChainAddress(account_id=acc.id, chain="ethereum",
                         address="0xeee", created_at=_utcnow(), updated_at=_utcnow()),
            ChainAddress(account_id=acc.id, chain="bitcoin",
                         address="bc1q",  created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await db.commit()

        def fake_dispatch(chain: str, alchemy_api_key: str | None):
            if chain == "ethereum":
                return FakeChainProvider("ethereum", [
                    BalanceItem(symbol="ETH", contract=None, quantity=Decimal("2"), decimals=18),
                ])
            if chain == "bitcoin":
                return FakeChainProvider("bitcoin", [
                    BalanceItem(symbol="BTC", contract=None, quantity=Decimal("0.5"), decimals=8),
                ])
            raise ValueError(chain)

        monkeypatch.setattr(orchestrator, "_dispatch_chain", fake_dispatch)

        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key="dummy")
        await db.commit()

        assert summary.account_type == "crypto_wallet"
        assert summary.total_synced == 2
        assert summary.total_errors == 0
        assert len(summary.results) == 2

        hs = (await db.execute(select(AssetHolding).where(AssetHolding.account_id == acc.id))).scalars().all()
        assert {h.chain for h in hs} == {"ethereum", "bitcoin"}
        assert all(h.last_synced_at for h in hs)

    async def test_partial_failure_does_not_abort_other_chains(self, db: AsyncSession, monkeypatch):
        acc = await _make_wallet(db, "Partial")
        db.add_all([
            ChainAddress(account_id=acc.id, chain="ethereum",
                         address="0xeee", created_at=_utcnow(), updated_at=_utcnow()),
            ChainAddress(account_id=acc.id, chain="bitcoin",
                         address="bc1q", created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await db.commit()

        def fake_dispatch(chain: str, alchemy_api_key: str | None):
            if chain == "ethereum":
                return FakeChainProvider("ethereum", raises=RuntimeError("rate limit"))
            return FakeChainProvider("bitcoin", [
                BalanceItem(symbol="BTC", contract=None, quantity=Decimal("0.25"), decimals=8),
            ])

        monkeypatch.setattr(orchestrator, "_dispatch_chain", fake_dispatch)
        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key="x")
        await db.commit()

        assert summary.total_errors == 1
        eth = next(r for r in summary.results if r.chain == "ethereum")
        btc = next(r for r in summary.results if r.chain == "bitcoin")
        assert eth.error and "rate limit" in eth.error
        assert btc.error is None and btc.synced == 1

        # The failing chain's chain_addresses row records the error.
        eth_addr = (await db.execute(
            select(ChainAddress).where(ChainAddress.chain == "ethereum",
                                       ChainAddress.account_id == acc.id)
        )).scalar_one()
        assert eth_addr.last_sync_status == "error"
        assert eth_addr.last_sync_error and "rate limit" in eth_addr.last_sync_error


class TestExchangeFlow:
    async def test_creds_decrypt_and_drive_provider(self, db: AsyncSession, monkeypatch):
        acc = await _make_exchange_account(db, "Bin")
        db.add(ExchangeConnection(
            account_id=acc.id, exchange="binance",
            api_key_enc=encrypt_str("real-key"),
            api_secret_enc=encrypt_str("real-secret"),
            api_passphrase_enc=None,
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await db.commit()

        captured = {}

        def fake_dispatch(exchange: str):
            class _P(FakeExchangeProvider):
                async def fetch_balances(self, api_key, api_secret, passphrase=None):
                    captured["api_key"] = api_key
                    captured["api_secret"] = api_secret
                    return [
                        BalanceItem(symbol="USDT", contract=None,
                                    quantity=Decimal("1000"), decimals=8),
                    ]
            return _P(exchange)

        monkeypatch.setattr(orchestrator, "_dispatch_exchange", fake_dispatch)
        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()

        assert captured["api_key"] == "real-key"
        assert captured["api_secret"] == "real-secret"
        assert summary.total_synced == 1
        hs = (await db.execute(select(AssetHolding).where(AssetHolding.account_id == acc.id))).scalars().all()
        assert len(hs) == 1 and hs[0].chain == ""

    async def test_bitget_passphrase_passed_through(self, db: AsyncSession, monkeypatch):
        acc = await _make_exchange_account(db, "Btg")
        db.add(ExchangeConnection(
            account_id=acc.id, exchange="bitget",
            api_key_enc=encrypt_str("k"),
            api_secret_enc=encrypt_str("s"),
            api_passphrase_enc=encrypt_str("pp"),
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await db.commit()

        captured = {}

        def fake_dispatch(exchange: str):
            class _P(FakeExchangeProvider):
                async def fetch_balances(self, api_key, api_secret, passphrase=None):
                    captured["passphrase"] = passphrase
                    return []
            return _P(exchange)

        monkeypatch.setattr(orchestrator, "_dispatch_exchange", fake_dispatch)
        await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)
        await db.commit()
        assert captured["passphrase"] == "pp"


class TestPriceRefreshDedupe:
    """Same asset on multiple chains must not write duplicate MarketPrice
    rows in one refresh — that previously triggered a UNIQUE constraint
    violation on (asset_id, source, quoted_at) and rolled back the whole
    sync session."""

    async def test_same_native_on_two_chains(self, db: AsyncSession, monkeypatch):
        from app.models import MarketPrice
        from sqlalchemy import select

        acc = await _make_wallet(db, "MultiChainNative")
        db.add_all([
            ChainAddress(account_id=acc.id, chain="ethereum",
                         address="0xeee", created_at=_utcnow(), updated_at=_utcnow()),
            ChainAddress(account_id=acc.id, chain="arbitrum",
                         address="0xeee", created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await db.commit()

        # Both chains report ETH (different quantities to be realistic).
        def fake_dispatch(chain, alchemy_api_key):
            qty = "1" if chain == "ethereum" else "2"
            return FakeChainProvider(chain, [
                BalanceItem(symbol="ETH", contract=None,
                            quantity=Decimal(qty), decimals=18),
            ])

        # Stub price fetcher so the test is deterministic + offline.
        async def fake_native_prices(symbols, *, http=None):
            return {
                s.strip().upper(): Decimal("3000")
                for s in symbols
                if s.strip().upper() == "ETH"
            }

        monkeypatch.setattr(orchestrator, "_dispatch_chain", fake_dispatch)
        monkeypatch.setattr(orchestrator, "fetch_native_prices", fake_native_prices)

        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key="dummy")
        await db.commit()

        assert summary.total_errors == 0, summary.results
        # Exactly ONE MarketPrice row for ETH — not two.
        rows = (
            await db.execute(select(MarketPrice).where(MarketPrice.source == "coingecko"))
        ).scalars().all()
        eth_prices = [r for r in rows if r.price == Decimal("3000")]
        assert len(eth_prices) == 1, f"expected 1 ETH price row, got {len(eth_prices)}"


class TestSafeErrorText:
    """Regression for Sec-H1 (2026-05-19): Alchemy API key embedded in
    httpx exception URLs must NOT leak into `last_sync_error` (which is
    persisted + echoed to the UI)."""

    def test_alchemy_key_redacted_in_status_error(self):
        import httpx
        from app.services.wallet_sync.orchestrator import _safe_error_text

        req = httpx.Request(
            "POST",
            "https://eth-mainnet.g.alchemy.com/v2/SECRETALCHEMYKEY12345abcde",
        )
        resp = httpx.Response(429, request=req)
        exc = httpx.HTTPStatusError("rate limited", request=req, response=resp)

        safe = _safe_error_text(exc)
        assert "SECRETALCHEMYKEY" not in safe
        assert "alchemy.com" not in safe
        assert safe == "HTTP 429"

    def test_alchemy_key_scrubbed_in_generic_exception_str(self):
        from app.services.wallet_sync.orchestrator import _safe_error_text

        exc = RuntimeError(
            "downstream error from /v2/SECRETKEY9876543210abcd123: timeout"
        )
        safe = _safe_error_text(exc)
        assert "SECRETKEY" not in safe
        assert "/v2/<redacted>" in safe

    def test_binance_signature_scrubbed(self):
        from app.services.wallet_sync.orchestrator import _safe_error_text

        exc = RuntimeError(
            "bad request: signature=abc123def456 returned 400"
        )
        safe = _safe_error_text(exc)
        assert "abc123def456" not in safe
        assert "signature=<redacted>" in safe

    def test_network_error_class_name(self):
        import httpx
        from app.services.wallet_sync.orchestrator import _safe_error_text

        exc = httpx.ConnectError("nope")
        safe = _safe_error_text(exc)
        assert "ConnectError" in safe
        assert "upstream connection error" in safe

    def test_empty_exception_falls_back_to_class_name(self):
        from app.services.wallet_sync.orchestrator import _safe_error_text

        class MyCustomError(Exception):
            pass
        safe = _safe_error_text(MyCustomError())
        assert safe == "MyCustomError"

    async def test_log_emission_uses_safe_text_not_repr(
        self, db, monkeypatch, caplog
    ):
        """Regression for V5-P1-6: log lines must NOT include
        repr(exc) — httpx exception repr leaks API keys / signatures."""
        import logging
        from app.models import ChainAddress

        acc = await _make_wallet(db, "LogScrub")
        db.add(ChainAddress(
            account_id=acc.id, chain="ethereum",
            address="0xLogScrubAddress", created_at=_utcnow(), updated_at=_utcnow(),
        ))
        await db.commit()

        # Provider raises an exception whose repr embeds an Alchemy key.
        def fake_dispatch(chain, alchemy_api_key):
            return FakeChainProvider(
                chain,
                raises=RuntimeError(
                    "https://eth-mainnet.g.alchemy.com/v2/SECRET99887766aabbcc rejected"
                ),
            )
        monkeypatch.setattr(orchestrator, "_dispatch_chain", fake_dispatch)

        with caplog.at_level(logging.WARNING):
            await orchestrator.sync_account(db, acc.id, alchemy_api_key="dummy")
            await db.commit()

        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRET99887766" not in joined, (
            "Alchemy API key leaked into log output via raw exception repr"
        )


class TestEdges:
    async def test_unknown_account_raises(self, db: AsyncSession):
        with pytest.raises(ValueError):
            await orchestrator.sync_account(db, 99999, alchemy_api_key=None)

    async def test_account_wrong_type_raises(self, db: AsyncSession):
        acc = Account(
            name="Bank", type="bank", currency="EUR",
            initial_balance=Decimal("0"), is_active=True,
            created_at=_utcnow(), updated_at=_utcnow(),
        )
        db.add(acc); await db.commit()
        with pytest.raises(ValueError):
            await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)

    async def test_crypto_wallet_with_no_addresses_returns_empty_summary(
        self, db: AsyncSession
    ):
        acc = await _make_wallet(db, "EmptyWallet")
        summary = await orchestrator.sync_account(db, acc.id, alchemy_api_key=None)
        assert summary.account_type == "crypto_wallet"
        assert summary.total_synced == 0
        assert summary.total_errors == 0
        assert summary.results == []
