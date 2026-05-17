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
    "llm_model": "gemini-2.5-flash",
    "llm_monthly_usd_budget": "5.0",
    "llm_confidence_threshold": "0.7",
    "llm_use_grounding": "true",
    "llm_max_notes_in_prompt": "20",
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
    return LLMSettings(
        enabled=enabled,
        provider=provider,
        model=model,
        monthly_usd_budget=budget,
        confidence_threshold=threshold,
        use_grounding=grounding,
        max_notes_in_prompt=max_notes,
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


async def get_gemini_api_key(db: AsyncSession) -> str | None:
    """Resolve the Gemini API key. UI-stored value wins over env so users
    can override deployment defaults without touching the .env file.

    Returns None when neither source has a key set.
    """
    from app.core.config import get_settings as _get_app_settings

    stored = await get_setting(db, "gemini_api_key", default=None)
    if stored:
        return stored
    env_key = _get_app_settings().gemini_api_key
    return env_key or None


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
