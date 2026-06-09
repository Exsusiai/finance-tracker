"""Encryption-credential health check.

Regression guard for ERR-20260607-001: when FINANCE_BANK_ENCRYPTION_KEY
changes, every previously-encrypted credential (Gemini API key, CEX API
key/secret/passphrase) becomes undecryptable. The old behaviour was a
SILENT failure — LLM classification quietly abstained and CEX sync threw
at decrypt time, with no startup signal telling the user the key rotated.

`verify_credentials_health` surfaces exactly which stored credentials no
longer decrypt, so lifespan can log a loud warning and the settings
endpoints can show "key changed, please re-enter" instead of "not set".
"""

from __future__ import annotations

import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")
# A valid 32-byte hex key so encrypt/decrypt round-trips in-test.
_GOOD_KEY = "11" * 32
os.environ["FINANCE_BANK_ENCRYPTION_KEY"] = _GOOD_KEY

from app.db import Base  # noqa: E402
from app.models import Account, ExchangeConnection, _utcnow_str  # noqa: E402
from app.services import app_settings as app_settings_svc  # noqa: E402
from app.services.bank_sync.crypto import encrypt_str  # noqa: E402
from app.services.security_health import verify_credentials_health  # noqa: E402


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


async def _make_exchange_account(db: AsyncSession, name: str) -> Account:
    a = Account(
        name=name, type="exchange", currency="USDT",
        initial_balance=0, is_active=True,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(a)
    await db.flush()
    return a


class TestHealthyCredentials:
    async def test_all_good_returns_empty_stale_list(self, db: AsyncSession):
        await app_settings_svc.set_gemini_api_key(db, "AIzaGoodKey123")
        acc = await _make_exchange_account(db, "Binance-OK")
        db.add(ExchangeConnection(
            account_id=acc.id, exchange="binance",
            api_key_enc=encrypt_str("k"), api_secret_enc=encrypt_str("s"),
            created_at=_utcnow_str(), updated_at=_utcnow_str(),
        ))
        await db.commit()

        report = await verify_credentials_health(db)
        assert report.stale == []
        assert report.ok_count >= 2


class TestStaleCredentials:
    async def test_undecryptable_gemini_key_flagged(self, db: AsyncSession):
        # Simulate a key encrypted under a DIFFERENT key: store a blob that
        # current key can't open. Easiest: a structurally-valid base64 that
        # isn't real ciphertext for this key.
        from app.services.app_settings import set_setting
        bad_blob = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # decrypt -> InvalidTag
        await set_setting(db, "gemini_api_key_enc", bad_blob)
        await db.commit()

        report = await verify_credentials_health(db)
        assert any("gemini" in s.lower() for s in report.stale), report.stale

    async def test_undecryptable_exchange_creds_flagged(self, db: AsyncSession):
        acc = await _make_exchange_account(db, "Bitget-Stale")
        db.add(ExchangeConnection(
            account_id=acc.id, exchange="bitget",
            api_key_enc="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            api_secret_enc="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            created_at=_utcnow_str(), updated_at=_utcnow_str(),
        ))
        await db.commit()

        report = await verify_credentials_health(db)
        assert any("bitget" in s.lower() or "exchange" in s.lower()
                   for s in report.stale), report.stale

    async def test_never_raises_on_missing_key_setting(self, db: AsyncSession):
        # No gemini row, no exchange rows beyond prior tests — must not throw.
        report = await verify_credentials_health(db)
        assert isinstance(report.stale, list)
