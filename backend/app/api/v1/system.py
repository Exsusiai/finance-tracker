"""System routes — health, version, settings, backup."""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.config import get_settings
from app.db import get_db, async_session_factory
from app.schemas import (
    ApiSuccess,
    BackupInfo,
    SettingsOut,
    SettingsUpdate,
)

router = APIRouter()
settings = get_settings()


_auth = Annotated[str, Depends(require_auth)]


@router.get("/settings", response_model=ApiSuccess[SettingsOut])
async def get_settings_route(_token: _auth):
    """Get current application settings."""
    return ApiSuccess(data=SettingsOut(
        base_currency=settings.base_currency,
        market_refresh_crypto_sec=settings.market_refresh_crypto_sec,
        market_refresh_stock_sec=settings.market_refresh_stock_sec,
        market_refresh_fx_sec=settings.market_refresh_fx_sec,
        market_refresh_gold_sec=settings.market_refresh_gold_sec,
    ))


@router.patch("/settings", response_model=ApiSuccess[SettingsOut])
async def update_settings(
    body: SettingsUpdate,
    _token: _auth,
):
    """Update application settings (in-memory only for this session)."""
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        # Update cached settings
        setattr(settings, key, value)

    return ApiSuccess(data=SettingsOut(
        base_currency=settings.base_currency,
        market_refresh_crypto_sec=settings.market_refresh_crypto_sec,
        market_refresh_stock_sec=settings.market_refresh_stock_sec,
        market_refresh_fx_sec=settings.market_refresh_fx_sec,
        market_refresh_gold_sec=settings.market_refresh_gold_sec,
    ))


@router.post("/backup", response_model=ApiSuccess[BackupInfo])
async def trigger_backup(
    _token: _auth,
):
    """Trigger an immediate SQLite database backup."""
    db_path = settings.db_path
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    backup_dir = settings.backup_dir
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"finance_{timestamp}.db"
    backup_path = backup_dir / backup_filename

    # Use SQLite backup API
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(backup_path))
        source.backup(dest)
        dest.close()
    finally:
        source.close()

    file_size = backup_path.stat().st_size
    return ApiSuccess(data=BackupInfo(
        filename=backup_filename,
        size_bytes=file_size,
        created_at=timestamp,
    ))


@router.get("/scheduler/status", response_model=ApiSuccess[dict])
async def get_scheduler_status(_token: _auth):
    """Snapshot of registered background jobs and their last-run outcome."""
    from app.services.market_data.scheduler import scheduler_status
    return ApiSuccess(data=scheduler_status())


@router.post("/refresh-matching", response_model=ApiSuccess[dict])
async def refresh_matching(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Re-run the full ingestion pipeline against the entire database.

    Mental model: "re-import every PDF from scratch" — re-detect type,
    re-classify category, re-pair across accounts, re-enqueue anything still
    untagged. Idempotent.

    Manual edits are respected: rows with `source='manual'` or `user_note`
    are NEVER re-classified — those carry an authoritative user choice.

    The 10-step pipeline lives in `services/refresh_matching/`; this route
    just wraps it in a savepoint and resets counters on exception so
    callers don't see mid-flight numbers in error logs.
    """
    from app.services.refresh_matching import RefreshContext, run_full_pipeline

    ctx = RefreshContext(db=db)
    try:
        async with db.begin_nested():
            await run_full_pipeline(ctx)
    except Exception:
        for k in ctx.summary:
            ctx.summary[k] = 0
        raise

    # Dispatch LLM tasks AFTER commit so they observe the committed state
    # when they open their own sessions. Without the explicit commit here,
    # tasks that read `tx_id` would see pre-pipeline state (the outer
    # session hasn't committed yet — get_db only commits on return).
    # Only count rows actually dispatched: if LLM is disabled / no key,
    # we don't fire the tasks (they'd just be no-ops) and the summary
    # accurately shows 0 so the user knows the rows fell through to inbox.
    if ctx.llm_targets:
        from app.services import app_settings as app_settings_svc
        runtime = await app_settings_svc.get_llm_settings(db)
        api_key = await app_settings_svc.get_gemini_api_key(db)
        if runtime.enabled and api_key:
            await db.commit()
            from app.services.ingestion import _dispatch_llm_classification
            await _dispatch_llm_classification(list(ctx.llm_targets))
            ctx.summary["llm_dispatched"] = len(ctx.llm_targets)

    return ApiSuccess(data=ctx.summary)



@router.get("/backups", response_model=ApiSuccess[list[BackupInfo]])
async def list_backups(
    _token: _auth,
):
    """List existing database backups."""
    backup_dir = settings.backup_dir
    backups = []

    if backup_dir.exists():
        for f in sorted(backup_dir.glob("finance_*.db"), reverse=True):
            stat = f.stat()
            backups.append(BackupInfo(
                filename=f.name,
                size_bytes=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))

    return ApiSuccess(data=backups)
