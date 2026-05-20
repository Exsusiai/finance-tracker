"""A2: _get_or_create_asset honours (chain, contract) identity.

The lookup precedence per decisions 2026-05-20:
  1. Onchain token (contract present) → exact match on
     (asset_class='crypto', symbol, chain, contract). Misses create
     a chain+contract-specific row.
  2. Native coin (no contract) → match on (..., symbol, '', ''). Per
     decision #1, ETH from chain='ethereum' and from chain='arbitrum'
     share ONE row with chain=''.
  3. Symbol fallback (no contract, found nothing at chain=''): never
     trips today because step 2 already normalises chain to '' for
     contractless items, but the test guards that future refactors
     don't break legacy manually-created Asset rows.

Tests are written BEFORE implementation lands (RED → GREEN).
"""

from __future__ import annotations

import os
from decimal import Decimal

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


def _utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _make_account(db: AsyncSession, name: str) -> Account:
    a = Account(
        name=name, type="crypto_wallet", currency="USDT",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utc(), updated_at=_utc(),
    )
    db.add(a)
    await db.flush()
    return a


async def _assets_by_symbol(db: AsyncSession, sym: str) -> list[Asset]:
    return list(
        (await db.execute(select(Asset).where(Asset.symbol == sym))).scalars()
    )


# ─── Native coins share one row across chains (decision #1) ──────────────


class TestNativeUnified:
    async def test_eth_on_two_chains_creates_one_asset(self, db: AsyncSession):
        a = await _make_account(db, "Wallet-ETH-x2")
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(symbol="ETH", contract=None,
                        quantity=Decimal("1"), decimals=18),
        ])
        await apply_balance_snapshot(db, a.id, "arbitrum", [
            BalanceItem(symbol="ETH", contract=None,
                        quantity=Decimal("2"), decimals=18),
        ])
        await db.commit()

        eths = await _assets_by_symbol(db, "ETH")
        assert len(eths) == 1, "ETH on L1 and L2 must share one Asset row"
        assert eths[0].chain == ""
        assert eths[0].contract == ""

        # But holdings are still per-chain (the chain discriminator
        # lives on AssetHolding, not on Asset for native coins).
        holdings = (
            await db.execute(select(AssetHolding).where(AssetHolding.account_id == a.id))
        ).scalars().all()
        assert {h.chain for h in holdings} == {"ethereum", "arbitrum"}


# ─── On-chain tokens split per chain+contract ────────────────────────────


class TestOnchainSplit:
    async def test_usdt_on_two_chains_creates_two_assets(self, db: AsyncSession):
        a = await _make_account(db, "Wallet-USDT-x2")
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(
                symbol="USDT",
                contract="0xdac17f958d2ee523a2206206994597c13d831ec7",
                quantity=Decimal("100"), decimals=6,
            ),
        ])
        await apply_balance_snapshot(db, a.id, "arbitrum", [
            BalanceItem(
                symbol="USDT",
                contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
                quantity=Decimal("50"), decimals=6,
            ),
        ])
        await db.commit()

        usdts = await _assets_by_symbol(db, "USDT")
        assert len(usdts) == 2, "USDT on Ethereum vs Arbitrum must be 2 Assets"
        chains = sorted(a.chain for a in usdts)
        assert chains == ["arbitrum", "ethereum"]

    async def test_usdc_two_contracts_same_chain_creates_two_assets(
        self, db: AsyncSession
    ):
        """Arbitrum has both native USDC and bridged USDC.E — different
        contracts, technically different liquidity. Two rows."""
        a = await _make_account(db, "Wallet-USDC-Arb")
        await apply_balance_snapshot(db, a.id, "arbitrum", [
            BalanceItem(
                symbol="USDC",
                contract="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                quantity=Decimal("10"), decimals=6,
            ),
            BalanceItem(
                symbol="USDC",
                contract="0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",
                quantity=Decimal("20"), decimals=6,
            ),
        ])
        await db.commit()
        usdcs = await _assets_by_symbol(db, "USDC")
        assert len(usdcs) == 2
        contracts = sorted(a.contract for a in usdcs)
        assert contracts[0].startswith("0xaf88")
        assert contracts[1].startswith("0xff97")


# ─── Re-sync is idempotent on chain-specific identity ────────────────────


class TestResyncIdempotent:
    async def test_resync_does_not_duplicate(self, db: AsyncSession):
        a = await _make_account(db, "Wallet-Resync")
        item = BalanceItem(
            symbol="USDT",
            contract="0xdac17f958d2ee523a2206206994597c13d831ec7",
            quantity=Decimal("1"), decimals=6,
        )
        await apply_balance_snapshot(db, a.id, "ethereum", [item])
        await apply_balance_snapshot(db, a.id, "ethereum", [item])
        await db.commit()

        usdts = await _assets_by_symbol(db, "USDT")
        eth_usdts = [a for a in usdts if a.chain == "ethereum"]
        assert len(eth_usdts) == 1


# ─── Legacy / manual Asset fallback ──────────────────────────────────────


class TestSymbolFallback:
    async def test_legacy_native_row_reused_when_no_contract(
        self, db: AsyncSession
    ):
        """User manually added 'GOLD-Token' with chain='', contract=''.
        Later a sync surfaces the same symbol with no contract — should
        reuse the manual row, not create a duplicate."""
        manual = Asset(
            symbol="GOLD-Token", name="Manual Gold", asset_class="crypto",
            currency="USDT", chain="", contract="",
            created_at=_utc(), updated_at=_utc(),
        )
        db.add(manual)
        await db.commit()

        a = await _make_account(db, "Wallet-Manual")
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(symbol="GOLD-Token", contract=None,
                        quantity=Decimal("5"), decimals=18),
        ])
        await db.commit()
        rows = await _assets_by_symbol(db, "GOLD-Token")
        assert len(rows) == 1, (
            "Native sync must reuse the existing chain='' row instead of "
            "creating a parallel one"
        )
        assert rows[0].id == manual.id

    async def test_legacy_native_row_NOT_reused_when_contract_present(
        self, db: AsyncSession
    ):
        """If the legacy row has chain='', contract='' and the sync
        brings an onchain CONTRACT version of the same symbol, those
        are LEGITIMATELY different rows — the legacy might be a
        manual/CEX placeholder, the onchain is a real contract."""
        manual = Asset(
            symbol="MYTOK", name="Manual Mytok", asset_class="crypto",
            currency="USDT", chain="", contract="",
            created_at=_utc(), updated_at=_utc(),
        )
        db.add(manual)
        await db.commit()

        a = await _make_account(db, "Wallet-MYTOK")
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(
                symbol="MYTOK",
                contract="0x" + "11" * 20,
                quantity=Decimal("7"), decimals=18,
            ),
        ])
        await db.commit()
        rows = await _assets_by_symbol(db, "MYTOK")
        assert len(rows) == 2
        ids = {r.id for r in rows}
        assert manual.id in ids


# ─── Spam still filtered before Asset creation ───────────────────────────


class TestSpamStillFiltered:
    async def test_spam_token_does_not_create_asset(self, db: AsyncSession):
        a = await _make_account(db, "Wallet-Spam")
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(
                symbol="VISIT FOO.XYZ TO CLAIM REWARD",
                contract="0x" + "22" * 20,
                quantity=Decimal("1000"), decimals=18,
            ),
        ])
        await db.commit()
        rows = await _assets_by_symbol(db, "VISIT FOO.XYZ TO CLAIM REWARD")
        assert rows == []


# ─── V6-P1-2: contract case sensitivity by chain ─────────────────────────


class TestContractCaseSensitivity:
    async def test_evm_contract_lowercased(self, db: AsyncSession):
        """EVM hex addresses are case-insensitive — UPPER and lower
        forms must collapse to one Asset row."""
        a = await _make_account(db, "Wallet-Case-Evm")
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(symbol="UNI", contract="0xAbCdEf0123456789aBcDeF0123456789AbCdEf01",
                        quantity=Decimal("1"), decimals=18),
        ])
        await apply_balance_snapshot(db, a.id, "ethereum", [
            BalanceItem(symbol="UNI", contract="0xABCDEF0123456789ABCDEF0123456789ABCDEF01",
                        quantity=Decimal("2"), decimals=18),
        ])
        await db.commit()
        unis = await _assets_by_symbol(db, "UNI")
        assert len(unis) == 1, "EVM contract case differences must collapse"
        assert unis[0].contract == "0xabcdef0123456789abcdef0123456789abcdef01"

    async def test_solana_mint_case_preserved(self, db: AsyncSession):
        """Solana SPL mints are case-sensitive base58 — DON'T lower-case
        them or CoinGecko `token_price/solana?contract_addresses=...`
        misses the token and prices come back as None."""
        a = await _make_account(db, "Wallet-Case-Sol")
        # USDC on Solana — real mint, mixed case must survive verbatim.
        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        await apply_balance_snapshot(db, a.id, "solana", [
            BalanceItem(symbol="USDC", contract=mint,
                        quantity=Decimal("100"), decimals=6),
        ])
        await db.commit()
        # Module-scoped DB shares state with prior USDC tests on other chains
        # — filter to the Solana row we just created.
        sol_rows = [a for a in await _assets_by_symbol(db, "USDC") if a.chain == "solana"]
        assert len(sol_rows) == 1
        assert sol_rows[0].contract == mint, (
            f"Solana mint must be stored verbatim, got {sol_rows[0].contract!r}"
        )

    async def test_tron_contract_case_preserved(self, db: AsyncSession):
        """Tron contracts (T-prefixed base58) are also case-sensitive."""
        a = await _make_account(db, "Wallet-Case-Tron")
        contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT-TRC20
        await apply_balance_snapshot(db, a.id, "tron", [
            BalanceItem(symbol="USDT", contract=contract,
                        quantity=Decimal("50"), decimals=6),
        ])
        await db.commit()
        usdts = await _assets_by_symbol(db, "USDT")
        assert any(a.contract == contract for a in usdts), (
            "Tron contract must survive case-preserving — found "
            f"{[a.contract for a in usdts]}"
        )


# ─── V6-P1-3: AccountCreate forces crypto/exchange currency=USDT ─────────


class TestCryptoExchangeCurrencyValidator:
    """Schema-level guard: backend rejects crypto_wallet/exchange accounts
    that don't use USDT, because /accounts/balances would add USDT-priced
    holdings to a wrongly-labelled currency bucket otherwise."""

    def test_crypto_wallet_with_eur_rejected(self):
        from pydantic import ValidationError
        from app.schemas import AccountCreate
        import pytest as _pytest
        with _pytest.raises(ValidationError):
            AccountCreate(name="Bad", type="crypto_wallet", currency="EUR")

    def test_exchange_with_cny_rejected(self):
        from pydantic import ValidationError
        from app.schemas import AccountCreate
        import pytest as _pytest
        with _pytest.raises(ValidationError):
            AccountCreate(name="Bad", type="exchange", currency="CNY")

    def test_crypto_wallet_with_usdt_accepted(self):
        from app.schemas import AccountCreate
        AccountCreate(name="Good", type="crypto_wallet", currency="USDT")

    def test_bank_with_eur_still_works(self):
        from app.schemas import AccountCreate
        AccountCreate(name="N26", type="bank", currency="EUR")
