"""Notion sync API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.services.notion_sync import NotionSyncService

logger = logging.getLogger(__name__)
router = APIRouter()


class SyncResponse(BaseModel):
    success: bool
    message: str
    stats: dict | None = None


class SetupResponse(BaseModel):
    success: bool
    database_ids: dict[str, str] | None = None
    message: str


def _get_notion_service() -> NotionSyncService:
    """Create a NotionSyncService from current settings."""
    settings = get_settings()
    return NotionSyncService(
        notion_token=settings.notion_token,
        transactions_db_id=settings.notion_transactions_db_id,
        cashflow_db_id=settings.notion_cashflow_db_id,
        asset_page_id=settings.notion_asset_page_id,
    )


@router.post("/sync", response_model=SyncResponse)
async def trigger_sync(
    since: Optional[str] = Query(
        None,
        description="ISO-8601 timestamp — only sync data modified after this time",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a Notion data sync.

    Syncs transactions, cash flow snapshots, and asset summary to Notion.
    """
    svc = _get_notion_service()

    if not svc.configured:
        raise HTTPException(
            status_code=400,
            detail="Notion sync not configured. Set NOTION_TOKEN and database IDs in .env",
        )

    try:
        stats = await svc.sync_all(db, since=since)
        return SyncResponse(
            success=len(stats.errors) == 0,
            message=stats.summary(),
            stats={
                "transactions_created": stats.transactions_created,
                "transactions_updated": stats.transactions_updated,
                "transactions_skipped": stats.transactions_skipped,
                "asset_summary_updated": stats.asset_summary_updated,
                "cashflow_created": stats.cashflow_entries_created,
                "cashflow_skipped": stats.cashflow_entries_skipped,
                "errors": stats.errors,
            },
        )
    except Exception as e:
        logger.exception("Notion sync failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync/transactions", response_model=SyncResponse)
async def sync_transactions_only(
    since: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Sync only transactions to Notion."""
    svc = _get_notion_service()
    if not svc.configured:
        raise HTTPException(status_code=400, detail="Notion sync not configured")

    stats = await svc.sync_transactions(db, since=since)
    return SyncResponse(
        success=len(stats.errors) == 0,
        message=stats.summary(),
    )


@router.post("/sync/cashflow", response_model=SyncResponse)
async def sync_cashflow_only(
    db: AsyncSession = Depends(get_db),
):
    """Sync only cash flow snapshots to Notion."""
    svc = _get_notion_service()
    if not svc.configured:
        raise HTTPException(status_code=400, detail="Notion sync not configured")

    stats = await svc.sync_cashflow(db)
    return SyncResponse(
        success=len(stats.errors) == 0,
        message=stats.summary(),
    )


@router.post("/sync/assets", response_model=SyncResponse)
async def sync_assets_only(
    db: AsyncSession = Depends(get_db),
):
    """Sync only asset summary to Notion."""
    svc = _get_notion_service()
    if not svc.configured:
        raise HTTPException(status_code=400, detail="Notion sync not configured")

    stats = await svc.sync_asset_summary(db)
    return SyncResponse(
        success=len(stats.errors) == 0,
        message=stats.summary(),
    )


@router.post("/setup", response_model=SetupResponse)
async def setup_notion_databases(
    parent_page_id: str = Query(..., description="Notion page ID to create databases under"),
):
    """One-time setup: create Notion databases for finance data.

    Returns the created database/page IDs. Save these to .env:
      NOTION_TRANSACTIONS_DB_ID=xxx
      NOTION_CASHFLOW_DB_ID=xxx
      NOTION_ASSET_PAGE_ID=xxx
    """
    svc = _get_notion_service()
    if not svc._token:
        raise HTTPException(status_code=400, detail="NOTION_TOKEN not configured")

    try:
        ids = await svc.ensure_databases(parent_page_id)
        return SetupResponse(
            success=True,
            database_ids=ids,
            message="Databases created. Save these IDs to your .env file.",
        )
    except Exception as e:
        logger.exception("Notion database setup failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status", response_model=SyncResponse)
async def notion_status():
    """Check Notion sync configuration status."""
    svc = _get_notion_service()
    configured_parts = []
    if svc._token:
        configured_parts.append("token")
    if svc._tx_db_id:
        configured_parts.append("transactions_db")
    if svc._cf_db_id:
        configured_parts.append("cashflow_db")
    if svc._asset_page_id:
        configured_parts.append("asset_page")

    if svc.configured:
        return SyncResponse(
            success=True,
            message=f"Configured: {', '.join(configured_parts)}",
        )
    else:
        return SyncResponse(
            success=False,
            message=f"Partially configured: {', '.join(configured_parts) if configured_parts else 'nothing'}",
        )
