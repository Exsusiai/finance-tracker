"""Typed accessor over the app_settings KV table.

The table is a simple `(key, value, updated_at)` store; this module wraps
the read/write paths so callers don't repeat parse/serialize boilerplate.

Defaults live in `_DEFAULTS`. `get_settings()` returns a frozen dataclass
view; `set_setting(key, value)` UPSERTs and `mass_set(dict)` updates many
keys atomically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, _utcnow_str

logger = structlog.get_logger(__name__)


# ─── Defaults ───────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, str] = {
    "llm_enabled": "false",
    "llm_provider": "gemini",
    # flash-lite has the widest free-tier quota; flash/2.0-flash exhaust fast.
    "llm_model": "gemini-2.5-flash-lite",
    "llm_monthly_usd_budget": "5.0",
    "llm_confidence_threshold": "0.7",
    "llm_use_grounding": "true",
    "llm_max_notes_in_prompt": "20",
    # Min seconds between LLM calls (single-worker queue pacing). ~5s ≈
    # 12 RPM, safely under the free-tier ~15 RPM cap so we never trip 429.
    "llm_min_interval_sec": "5",
}


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    provider: str
    model: str
    monthly_usd_budget: float
    confidence_threshold: float
    use_grounding: bool
    max_notes_in_prompt: int
    min_interval_sec: float


def _to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def _read_raw(db: AsyncSession, key: str, default: str) -> str:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        return default
    return row.value


async def get_llm_settings(db: AsyncSession) -> LLMSettings:
    enabled = _to_bool(await _read_raw(db, "llm_enabled", _DEFAULTS["llm_enabled"]))
    provider = await _read_raw(db, "llm_provider", _DEFAULTS["llm_provider"])
    model = await _read_raw(db, "llm_model", _DEFAULTS["llm_model"])
    budget = float(await _read_raw(db, "llm_monthly_usd_budget", _DEFAULTS["llm_monthly_usd_budget"]))
    threshold = float(await _read_raw(db, "llm_confidence_threshold", _DEFAULTS["llm_confidence_threshold"]))
    grounding = _to_bool(await _read_raw(db, "llm_use_grounding", _DEFAULTS["llm_use_grounding"]))
    max_notes = int(await _read_raw(db, "llm_max_notes_in_prompt", _DEFAULTS["llm_max_notes_in_prompt"]))
    interval = float(await _read_raw(db, "llm_min_interval_sec", _DEFAULTS["llm_min_interval_sec"]))
    return LLMSettings(
        enabled=enabled,
        provider=provider,
        model=model,
        monthly_usd_budget=budget,
        confidence_threshold=threshold,
        use_grounding=grounding,
        max_notes_in_prompt=max_notes,
        min_interval_sec=interval,
    )


async def get_setting(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        return _DEFAULTS.get(key, default)
    return row.value


async def set_setting(db: AsyncSession, key: str, value: Any) -> None:
    serialized = value if isinstance(value, str) else json.dumps(value) if not isinstance(value, (int, float, bool)) else str(value).lower() if isinstance(value, bool) else str(value)
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        db.add(AppSetting(key=key, value=serialized, updated_at=_utcnow_str()))
    else:
        row.value = serialized
        row.updated_at = _utcnow_str()
    await db.flush()


async def delete_setting(db: AsyncSession, key: str) -> bool:
    """Remove a setting row entirely. Returns True if a row was deleted."""
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def set_gemini_api_key(db: AsyncSession, raw_key: str) -> None:
    """Encrypt *raw_key* with AES-256-GCM and persist to ``gemini_api_key_enc``.

    Uses the same ``encrypt_str`` helper that exchange/bank credentials use so
    the key is never written in plaintext to the KV store.
    """
    from app.services.bank_sync.crypto import encrypt_str

    encrypted = encrypt_str(raw_key)
    await set_setting(db, "gemini_api_key_enc", encrypted)


async def get_gemini_api_key(db: AsyncSession) -> str | None:
    """Resolve the Gemini API key. Priority order:

    1. Encrypted DB row ``gemini_api_key_enc`` (set via Settings UI)
    2. Env / .env ``GEMINI_API_KEY`` (deployment default)

    Returns None when neither source has a key set.
    """
    from app.core.config import get_settings as _get_app_settings
    from app.services.bank_sync.crypto import decrypt_str

    encrypted = await get_setting(db, "gemini_api_key_enc", default=None)
    if encrypted:
        try:
            return decrypt_str(encrypted)
        except Exception:
            logger.warning("gemini_api_key_dec_failed", hint="key may be from different encryption key")

    env_key = _get_app_settings().gemini_api_key
    return env_key or None


async def _migrate_legacy_gemini_key_to_encrypted(db: AsyncSession) -> None:
    """One-shot migration: move plaintext ``gemini_api_key`` → ``gemini_api_key_enc``.

    Idempotent — skips if the plaintext row is already gone or if an
    encrypted row already exists (so a subsequent startup is a no-op).
    """
    plaintext_row = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key"))
    ).scalar_one_or_none()

    if plaintext_row is None:
        return  # nothing to migrate

    # Don't overwrite an already-encrypted row that arrived via a race or
    # a previous partial run.
    enc_row = (
        await db.execute(select(AppSetting).where(AppSetting.key == "gemini_api_key_enc"))
    ).scalar_one_or_none()

    if enc_row is None:
        await set_gemini_api_key(db, plaintext_row.value)
        logger.info("gemini_api_key_migrated_to_encrypted")

    # Always remove the plaintext row after a successful migration.
    await db.delete(plaintext_row)
    await db.flush()


async def seed_defaults(db: AsyncSession) -> int:
    """Insert default rows for any missing keys. Returns number inserted."""
    existing = {row.key for row in (await db.execute(select(AppSetting))).scalars().all()}
    inserted = 0
    for key, default in _DEFAULTS.items():
        if key not in existing:
            db.add(AppSetting(key=key, value=default, updated_at=_utcnow_str()))
            inserted += 1
    if inserted:
        await db.flush()
        logger.info("app_settings_seeded", count=inserted)
    return inserted
