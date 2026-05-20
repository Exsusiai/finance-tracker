"""P1-4 A4.1: holdings upsert logic for wallet/exchange sync.

Pure DB-layer test — no HTTP, no provider. Verifies the contract between
``BalanceItem`` lists and ``asset_holdings`` rows:

- Asset rows are deduped by ``(symbol, asset_class='crypto')``.
- BalanceItems without a symbol fall back to a contract-derived
  placeholder symbol; collisions across different contracts stay distinct.
- ``asset_holdings`` rows are keyed by ``(account_id, asset_id, chain)``.
- On re-sync, tokens that disappear get ``quantity=0`` + ``is_active=False``;
  reappearing tokens are reactivated.
- CEX flows use ``chain=""`` (orchestrator's choice — the helper just
  passes ``chain`` through).
"""

from __future__ import annotations

import os
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

from app.db import Base  # noqa: E402
from app.models import Account, Asset, AssetHolding  # noqa: E402
from app.services.crypto_sync import BalanceItem  # noqa: E402
from app.services.wallet_sync.upsert import apply_balance_snapshot  # noqa: E402


# ─── Test database setup ────────────────────────────────────────────────────

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


async def _make_account(db: AsyncSession, name: str) -> Account:
    acc = Account(
        name=name,
        type="crypto_wallet",
        currency="USDT",
        initial_balance=Decimal("0"),
        is_active=True,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(acc)
    await db.commit()
    return acc


async def _holdings(db: AsyncSession, account_id: int) -> list[AssetHolding]:
    rows = (
        await db.execute(
            select(AssetHolding).where(AssetHolding.account_id == account_id)
        )
    ).scalars().all()
    return list(rows)


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestFreshInsert:
    async def test_first_sync_creates_assets_and_holdings(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-Fresh")
        items = [
            BalanceItem(symbol="ETH",  contract=None, quantity=Decimal("2"),   decimals=18),
            BalanceItem(symbol="USDT", contract="0xdac17", quantity=Decimal("100"), decimals=6),
        ]
        n = await apply_balance_snapshot(db, acc.id, "ethereum", items)
        await db.commit()

        assert n == 2
        hs = await _holdings(db, acc.id)
        assert len(hs) == 2
        for h in hs:
            await db.refresh(h, ["asset"])
        by_symbol = {h.asset.symbol: h for h in hs}
        assert by_symbol["ETH"].quantity == Decimal("2")
        assert by_symbol["ETH"].chain == "ethereum"
        assert by_symbol["ETH"].is_active is True
        assert by_symbol["USDT"].quantity == Decimal("100")

    async def test_token_split_per_chain_contract(self, db: AsyncSession):
        """USDT on Ethereum and USDT on Arbitrum are DIFFERENT Asset rows
        — different contracts, potentially different prices.

        (A-sprint 2026-05-20: old behaviour merged them into one shared
        row sharing one price, which silently poisoned valuation when
        either contract's price moved off-peg or got hijacked. See
        V5-P1-1 / .learnings ERR-20260520 family.)"""
        acc = await _make_account(db, "Wallet-Reuse")
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [BalanceItem(symbol="USDT", contract="0xdac17", quantity=Decimal("100"), decimals=6)],
        )
        await apply_balance_snapshot(
            db, acc.id, "arbitrum",
            [BalanceItem(symbol="USDT", contract="0xfd086", quantity=Decimal("50"), decimals=6)],
        )
        await db.commit()

        assets = (await db.execute(select(Asset).where(Asset.symbol == "USDT"))).scalars().all()
        assert len(assets) == 2, "Same symbol on different chains must split"
        assert {a.chain for a in assets} == {"ethereum", "arbitrum"}
        # Contracts are lower-cased canonical form so equality is stable.
        assert {a.contract for a in assets} == {"0xdac17", "0xfd086"}

        hs = await _holdings(db, acc.id)
        assert len(hs) == 2
        chains = {h.chain for h in hs}
        assert chains == {"ethereum", "arbitrum"}

    async def test_no_symbol_falls_back_to_contract_placeholder(self, db: AsyncSession):
        """Solana SPL tokens come without a symbol — must not collide."""
        acc = await _make_account(db, "Wallet-NoSymbol")
        items = [
            BalanceItem(symbol=None, contract="Es9vMFrz123", quantity=Decimal("10"), decimals=6),
            BalanceItem(symbol=None, contract="EPjFWdd5456", quantity=Decimal("20"), decimals=6),
        ]
        await apply_balance_snapshot(db, acc.id, "solana", items)
        await db.commit()

        hs = await _holdings(db, acc.id)
        assert len(hs) == 2
        # Distinct asset rows (different contracts → different placeholders).
        asset_ids = {h.asset_id for h in hs}
        assert len(asset_ids) == 2


class TestResyncSemantics:
    async def test_disappearing_token_zeroed_and_inactive(self, db: AsyncSession):
        """Per user decision: missing token => quantity=0, is_active=False."""
        acc = await _make_account(db, "Wallet-Disappear")
        # Round 1: ETH + USDT
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [
                BalanceItem(symbol="ETH",  contract=None, quantity=Decimal("2"), decimals=18),
                BalanceItem(symbol="USDT", contract="0xdac17", quantity=Decimal("100"), decimals=6),
            ],
        )
        await db.commit()

        # Round 2: only ETH (USDT moved off-wallet)
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [BalanceItem(symbol="ETH", contract=None, quantity=Decimal("1.5"), decimals=18)],
        )
        await db.commit()

        hs = await _holdings(db, acc.id)
        for h in hs:
            await db.refresh(h, ["asset"])
        by_sym = {h.asset.symbol: h for h in hs}
        assert by_sym["ETH"].quantity == Decimal("1.5")
        assert by_sym["ETH"].is_active is True
        assert by_sym["USDT"].quantity == Decimal("0")
        assert by_sym["USDT"].is_active is False

    async def test_reappearing_token_reactivated(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-Reappear")
        # Round 1: USDT present
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [BalanceItem(symbol="USDT", contract="0xdac17", quantity=Decimal("100"), decimals=6)],
        )
        await db.commit()
        # Round 2: nothing — USDT gets zeroed
        await apply_balance_snapshot(db, acc.id, "ethereum", [])
        await db.commit()
        # Round 3: USDT back with new balance
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [BalanceItem(symbol="USDT", contract="0xdac17", quantity=Decimal("75"), decimals=6)],
        )
        await db.commit()

        hs = await _holdings(db, acc.id)
        for h in hs:
            await db.refresh(h, ["asset"])
        usdt = next(h for h in hs if h.asset.symbol == "USDT")
        assert usdt.quantity == Decimal("75")
        assert usdt.is_active is True

    async def test_resync_scoped_per_chain(self, db: AsyncSession):
        """A new sync of `ethereum` must not touch holdings on `arbitrum`."""
        acc = await _make_account(db, "Wallet-PerChain")
        await apply_balance_snapshot(
            db, acc.id, "ethereum",
            [BalanceItem(symbol="USDT", contract="0xdac17", quantity=Decimal("100"), decimals=6)],
        )
        await apply_balance_snapshot(
            db, acc.id, "arbitrum",
            [BalanceItem(symbol="USDT", contract="0xfd086", quantity=Decimal("50"), decimals=6)],
        )
        await db.commit()

        # Re-sync ethereum to empty. arbitrum row MUST be untouched.
        await apply_balance_snapshot(db, acc.id, "ethereum", [])
        await db.commit()

        hs = await _holdings(db, acc.id)
        eth = next(h for h in hs if h.chain == "ethereum")
        arb = next(h for h in hs if h.chain == "arbitrum")
        assert eth.quantity == Decimal("0") and eth.is_active is False
        assert arb.quantity == Decimal("50") and arb.is_active is True


class TestCEXFlow:
    async def test_chain_empty_string_for_cex(self, db: AsyncSession):
        acc = await _make_account(db, "Wallet-Binance")
        await apply_balance_snapshot(
            db, acc.id, "",
            [
                BalanceItem(symbol="BTC", contract=None, quantity=Decimal("0.5"), decimals=8),
                BalanceItem(symbol="USDT", contract=None, quantity=Decimal("1000"), decimals=8),
            ],
        )
        await db.commit()
        hs = await _holdings(db, acc.id)
        assert {h.chain for h in hs} == {""}
        assert {h.quantity for h in hs} == {Decimal("0.5"), Decimal("1000")}
