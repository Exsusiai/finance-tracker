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
