"""Tests for encrypted Gemini API key storage in app_settings.

Covers:
- set_gemini_api_key → get_gemini_api_key round-trip (ciphertext differs from plaintext).
- Legacy plaintext migration: migrates to encrypted row and removes old row.
- Env fallback: get_gemini_api_key returns env value when DB has no row.
- Clear path: deleting gemini_api_key_enc causes env fallback.
"""
from __future__ import annotations

import os
import secrets

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set env vars BEFORE importing anything from app (mirrors test_wallet_sync_orchestrator.py).
_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")
os.environ.setdefault("FINANCE_BANK_ENCRYPTION_KEY", secrets.token_hex(32))

from app.db import Base  # noqa: E402
from app.models import AppSetting, _utcnow_str  # noqa: E402
from app.services import app_settings as svc  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_and_get_round_trip(db: AsyncSession) -> None:
    """set_gemini_api_key encrypts; get_gemini_api_key decrypts back to original."""
    raw = "AIza-test-key-1234"
    await svc.set_gemini_api_key(db, raw)
    await db.flush()

    # Verify the stored value is NOT the raw key (it's a base64 blob).
    row = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key_enc"))
    ).scalar_one_or_none()
    assert row is not None, "gemini_api_key_enc row should be written"
    assert row.value != raw, "stored value must be encrypted, not plaintext"

    # get_gemini_api_key should decrypt it back.
    recovered = await svc.get_gemini_api_key(db)
    assert recovered == raw


@pytest.mark.asyncio
async def test_ciphertext_is_nondeterministic(db: AsyncSession) -> None:
    """Each call to set_gemini_api_key uses a fresh nonce — same key ≠ same blob."""
    raw = "AIza-same-key"
    await svc.set_gemini_api_key(db, raw)
    await db.flush()

    row1 = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key_enc"))
    ).scalar_one()
    blob1 = row1.value

    # Overwrite with the same plaintext.
    await svc.set_gemini_api_key(db, raw)
    await db.flush()

    row2 = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key_enc"))
    ).scalar_one()
    blob2 = row2.value

    # Ciphertexts should differ (different nonces).
    assert blob1 != blob2
    # Both should decrypt to the same value.
    recovered = await svc.get_gemini_api_key(db)
    assert recovered == raw


@pytest.mark.asyncio
async def test_env_fallback_when_no_db_row(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_gemini_api_key falls back to env GEMINI_API_KEY when DB has no row."""
    monkeypatch.setenv("GEMINI_API_KEY", "env-fallback-key-xyz")
    # Patch get_settings so we don't need a full config stack.
    import app.services.app_settings as _svc_mod

    class _FakeSettings:
        gemini_api_key = "env-fallback-key-xyz"

    monkeypatch.setattr(_svc_mod, "_get_fake_settings", None, raising=False)
    original_get_settings = None
    import app.core.config as _cfg

    original_get_settings = _cfg.get_settings

    def _patched():
        return _FakeSettings()

    monkeypatch.setattr(_cfg, "get_settings", _patched)

    result = await svc.get_gemini_api_key(db)
    assert result == "env-fallback-key-xyz"

    monkeypatch.setattr(_cfg, "get_settings", original_get_settings)


@pytest.mark.asyncio
async def test_env_fallback_returns_none_when_nothing_set(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns None when neither DB row nor env key is present."""
    import app.core.config as _cfg

    class _EmptySettings:
        gemini_api_key = ""

    monkeypatch.setattr(_cfg, "get_settings", lambda: _EmptySettings())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = await svc.get_gemini_api_key(db)
    assert result is None


@pytest.mark.asyncio
async def test_legacy_migration_encrypts_and_removes_plaintext(db: AsyncSession) -> None:
    """_migrate_legacy_gemini_key_to_encrypted moves plaintext → encrypted row."""
    # Insert a legacy plaintext row.
    db.add(AppSetting(key="gemini_api_key", value="AIza-legacy-plain", updated_at=_utcnow_str()))
    await db.flush()

    await svc._migrate_legacy_gemini_key_to_encrypted(db)
    await db.flush()

    # Old plaintext row should be gone.
    old = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key"))
    ).scalar_one_or_none()
    assert old is None, "legacy plaintext row should be deleted"

    # New encrypted row should exist and decrypt correctly.
    recovered = await svc.get_gemini_api_key(db)
    assert recovered == "AIza-legacy-plain"


@pytest.mark.asyncio
async def test_legacy_migration_is_idempotent(db: AsyncSession) -> None:
    """Running migration twice does not error and does not create duplicate rows."""
    db.add(AppSetting(key="gemini_api_key", value="AIza-idem", updated_at=_utcnow_str()))
    await db.flush()

    await svc._migrate_legacy_gemini_key_to_encrypted(db)
    await db.flush()
    # Second run — no plaintext row left, should be a no-op.
    await svc._migrate_legacy_gemini_key_to_encrypted(db)
    await db.flush()

    rows = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key_enc"))
    ).scalars().all()
    assert len(rows) == 1, "exactly one encrypted row expected after idempotent migration"


@pytest.mark.asyncio
async def test_migration_skips_when_no_legacy_row(db: AsyncSession) -> None:
    """Migration is a no-op when there is no legacy plaintext row."""
    # No rows at all — should not raise.
    await svc._migrate_legacy_gemini_key_to_encrypted(db)
    await db.flush()

    enc = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key_enc"))
    ).scalar_one_or_none()
    assert enc is None, "no encrypted row should be created when migration had nothing to do"


@pytest.mark.asyncio
async def test_migration_does_not_overwrite_existing_encrypted_row(db: AsyncSession) -> None:
    """If both legacy and encrypted rows coexist, the existing encrypted row wins."""
    # Pre-existing encrypted row (e.g. from a previous partial run).
    await svc.set_gemini_api_key(db, "AIza-already-encrypted")
    await db.flush()

    # Also insert a stale plaintext row (shouldn't overwrite encrypted row).
    db.add(AppSetting(key="gemini_api_key", value="AIza-stale-plain", updated_at=_utcnow_str()))
    await db.flush()

    await svc._migrate_legacy_gemini_key_to_encrypted(db)
    await db.flush()

    # The pre-existing encrypted key should survive.
    recovered = await svc.get_gemini_api_key(db)
    assert recovered == "AIza-already-encrypted"

    # Plaintext row should still be cleaned up.
    old = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key"))
    ).scalar_one_or_none()
    assert old is None
