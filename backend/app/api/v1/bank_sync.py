"""Bank sync API routes — manage bank connections and trigger syncs."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.db import get_db
from app.models import Account
from app.models.bank_connection import BankConnection
from app.schemas import ApiSuccess, ErrorDetail
from app.schemas.bank_sync import (
    BankConnectionCreate,
    BankConnectionOut,
    BankConnectionUpdate,
    BankSyncStatusOut,
    CallbackResultOut,
    ConnectionCreateResult,
    GoCardlessSetupRequest,
    GoCardlessSetupResponse,
    InstitutionOut,
    SyncResultOut,
)
from app.services.bank_sync.engine import BankSyncEngine

logger = structlog.get_logger(__name__)

router = APIRouter()


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Setup ────────────────────────────────────────────────────────────────


@router.post("/setup", response_model=ApiSuccess[GoCardlessSetupResponse])
async def setup_gocardless(
    body: GoCardlessSetupRequest,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Configure GoCardless API credentials and verify connectivity."""
    engine = BankSyncEngine(db)
    result = await engine.setup_gocardless(body.secret_id, body.secret_key)
    return ApiSuccess(
        data=GoCardlessSetupResponse(
            setup_ok=result["success"],
            has_access_token=result.get("has_access_token", False),
            has_refresh_token=result.get("has_refresh_token", False),
            encrypted_credentials=result.get("encrypted_credentials"),
            error=result.get("error"),
        )
    )


# ─── Institutions ─────────────────────────────────────────────────────────


@router.get("/institutions", response_model=ApiSuccess[list[InstitutionOut]])
async def list_institutions(
    country: str = Query(..., min_length=2, max_length=2, description="ISO 3166-1 alpha-2"),
    encrypted_credentials: str = Query(..., description="Encrypted GoCardless credentials"),
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """List available financial institutions for a country.

    Requires encrypted GoCardless credentials (from /setup endpoint).
    """
    engine = BankSyncEngine(db)
    institutions = await engine.list_institutions(
        provider_name="gocardless",
        encrypted_creds=encrypted_credentials,
        country=country,
    )
    return ApiSuccess(data=[InstitutionOut(**inst) for inst in institutions])


# ─── Connections CRUD ────────────────────────────────────────────────────


@router.post("/connections", response_model=ApiSuccess[ConnectionCreateResult])
async def create_connection(
    body: BankConnectionCreate,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Create a new bank connection (initiate OAuth flow).

    Returns a link that the user should open to authenticate with their bank.
    After authentication, call POST /callback with the requisition_id.
    """
    engine = BankSyncEngine(db)

    # Get institution info for the connection record
    institutions = await engine.list_institutions(
        provider_name=body.provider,
        encrypted_creds=body.encrypted_credentials,
        country=body.redirect_url,  # We need country — get from institution
    )

    # Create requisition
    result = await engine.create_connection(
        provider_name=body.provider,
        encrypted_creds=body.encrypted_credentials,
        institution_id=body.institution_id,
        redirect_url=body.redirect_url,
        reference=body.reference,
        max_historical_days=body.max_historical_days,
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    # Store connection in DB
    # Try to find institution name from the requisition
    conn = BankConnection(
        provider=body.provider,
        institution_id=body.institution_id,
        institution_name=body.institution_id,  # Will update after callback
        institution_country="DE",  # Default, update after callback
        currency="EUR",  # Default, update after callback
        gc_requisition_id=result["requisition_id"],
        gc_agreement_id=result.get("agreement_id"),
        encrypted_creds=result.get("updated_credentials") or body.encrypted_credentials,
        status="connecting",
        metadata_json=json.dumps({
            "reference": body.reference,
            "max_historical_days": body.max_historical_days,
        }),
    )
    db.add(conn)
    await db.flush()  # Get the ID

    return ApiSuccess(
        data=ConnectionCreateResult(
            requisition_id=result["requisition_id"],
            link=result["link"],
            status=result.get("status", "CR"),
            agreement_id=result.get("agreement_id"),
            updated_credentials=result.get("updated_credentials"),
        ),
        meta={"connection_id": conn.id},
    )


@router.get("/connections", response_model=ApiSuccess[list[BankConnectionOut]])
async def list_connections(
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """List all bank connections."""
    stmt = (
        select(BankConnection)
        .where(BankConnection.deleted_at.is_(None))
        .order_by(BankConnection.created_at.desc())
    )
    result = await db.execute(stmt)
    connections = result.scalars().all()
    return ApiSuccess(data=[BankConnectionOut.model_validate(c) for c in connections])


@router.get(
    "/connections/{connection_id}", response_model=ApiSuccess[BankConnectionOut]
)
async def get_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Get a specific bank connection."""
    stmt = select(BankConnection).where(
        BankConnection.id == connection_id,
        BankConnection.deleted_at.is_(None),
    )
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return ApiSuccess(data=BankConnectionOut.model_validate(conn))


@router.patch(
    "/connections/{connection_id}", response_model=ApiSuccess[BankConnectionOut]
)
async def update_connection(
    connection_id: int,
    body: BankConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Update a bank connection."""
    stmt = select(BankConnection).where(
        BankConnection.id == connection_id,
        BankConnection.deleted_at.is_(None),
    )
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if body.account_id is not None:
        conn.account_id = body.account_id
    if body.sync_interval_hours is not None:
        conn.sync_interval_hours = body.sync_interval_hours
    if body.encrypted_credentials is not None:
        conn.encrypted_creds = body.encrypted_credentials
    conn.updated_at = _utcnow_str()

    return ApiSuccess(data=BankConnectionOut.model_validate(conn))


@router.delete("/connections/{connection_id}", response_model=ApiSuccess[dict])
async def delete_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Delete a bank connection and revoke bank access."""
    stmt = select(BankConnection).where(
        BankConnection.id == connection_id,
        BankConnection.deleted_at.is_(None),
    )
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Try to revoke at the provider level
    revoked = False
    if conn.gc_requisition_id and conn.encrypted_creds:
        try:
            engine = BankSyncEngine(db)
            revoked = await engine.revoke_connection(
                provider_name=conn.provider,
                encrypted_creds=conn.encrypted_creds,
                requisition_id=conn.gc_requisition_id,
            )
        except Exception as e:
            logger.warning(
                "bank_sync_revoke_failed",
                connection_id=connection_id,
                error=str(e),
            )

    # Soft delete
    conn.deleted_at = _utcnow_str()
    conn.status = "revoked"
    conn.updated_at = _utcnow_str()

    return ApiSuccess(data={"revoked": revoked, "id": connection_id})


# ─── Callback ─────────────────────────────────────────────────────────────


@router.post("/callback", response_model=ApiSuccess[CallbackResultOut])
async def handle_callback(
    requisition_id: str = Query(...),
    connection_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Handle OAuth callback after user authenticates with their bank.

    Updates the bank connection with linked account information.
    """
    stmt = select(BankConnection).where(
        BankConnection.id == connection_id,
        BankConnection.deleted_at.is_(None),
    )
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if not conn.encrypted_creds:
        raise HTTPException(
            status_code=400, detail="Connection has no credentials"
        )

    engine = BankSyncEngine(db)
    result = await engine.handle_callback(
        provider_name=conn.provider,
        encrypted_creds=conn.encrypted_creds,
        requisition_id=requisition_id,
    )

    if result["linked"]:
        conn.status = "active"
        conn.gc_account_ids_json = json.dumps(
            [a["id"] for a in result["accounts"]]
        )
        # Update institution info from accounts
        if result["accounts"]:
            first_acc = result["accounts"][0]
            if first_acc.get("currency"):
                conn.currency = first_acc["currency"]
        if result.get("agreement_id"):
            conn.gc_agreement_id = result["agreement_id"]

        # Store updated credentials (may have refreshed tokens)
        updated_creds = engine._save_provider_state(
            conn.provider,
            engine._create_provider(conn.provider, conn.encrypted_creds),
            conn.encrypted_creds,
        )
        conn.encrypted_creds = updated_creds
    else:
        conn.status = "error"
        conn.last_sync_error = result.get("error", "Callback failed")

    conn.updated_at = _utcnow_str()

    return ApiSuccess(data=CallbackResultOut(**result))


# ─── Sync ─────────────────────────────────────────────────────────────────


@router.post(
    "/connections/{connection_id}/sync", response_model=ApiSuccess[SyncResultOut]
)
async def sync_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Manually trigger a sync for a bank connection.

    Requires the connection to be linked to a local account (account_id set).
    """
    stmt = select(BankConnection).where(
        BankConnection.id == connection_id,
        BankConnection.deleted_at.is_(None),
    )
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if conn.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Connection is not active (status: {conn.status})",
        )

    if not conn.account_id:
        raise HTTPException(
            status_code=400,
            detail="Connection is not linked to a local account. "
            "Set account_id first via PATCH /connections/{id}.",
        )

    if not conn.encrypted_creds or not conn.gc_account_ids_json:
        raise HTTPException(
            status_code=400,
            detail="Connection has no credentials or account IDs",
        )

    # Verify the local account exists
    acc_stmt = select(Account).where(Account.id == conn.account_id)
    if not (await db.execute(acc_stmt)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Local account not found")

    gc_account_ids = json.loads(conn.gc_account_ids_json)
    if not gc_account_ids:
        raise HTTPException(status_code=400, detail="No GoCardless account IDs")

    # Sync transactions for the first account
    engine = BankSyncEngine(db)
    result = await engine.sync_transactions(
        provider_name=conn.provider,
        encrypted_creds=conn.encrypted_creds,
        gc_account_id=gc_account_ids[0],
        local_account_id=conn.account_id,
        last_sync_at=conn.last_sync_at,
    )

    # Update connection state
    conn.last_sync_at = _utcnow_str()
    conn.last_sync_status = "success" if result.success else "error"
    conn.last_sync_error = result.error
    conn.next_sync_at = result.next_sync_at
    conn.total_transactions += result.transactions_new
    conn.updated_at = _utcnow_str()

    # Update encrypted creds (may have refreshed tokens)
    try:
        conn.encrypted_creds = engine._save_provider_state(
            conn.provider,
            engine._create_provider(conn.provider, conn.encrypted_creds),
            conn.encrypted_creds,
        )
    except Exception:
        pass  # Non-critical, don't fail the sync

    return ApiSuccess(
        data=SyncResultOut(
            success=result.success,
            transactions_new=result.transactions_new,
            transactions_existing=result.transactions_existing,
            transactions_pending=result.transactions_pending,
            balance_amount=str(result.balance.amount) if result.balance else None,
            balance_currency=result.balance.currency if result.balance else None,
            error=result.error,
            next_sync_at=result.next_sync_at,
            rate_limit_remaining=result.rate_limit_remaining,
        )
    )


@router.post(
    "/connections/{connection_id}/reconnect",
    response_model=ApiSuccess[ConnectionCreateResult],
)
async def reconnect_connection(
    connection_id: int,
    redirect_url: str = Query(
        default="http://localhost:3000/settings/bank-sync/callback",
    ),
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Re-authorize an expired or failed bank connection."""
    stmt = select(BankConnection).where(
        BankConnection.id == connection_id,
        BankConnection.deleted_at.is_(None),
    )
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if not conn.encrypted_creds:
        raise HTTPException(status_code=400, detail="No credentials stored")

    engine = BankSyncEngine(db)
    result = await engine.create_connection(
        provider_name=conn.provider,
        encrypted_creds=conn.encrypted_creds,
        institution_id=conn.institution_id,
        redirect_url=redirect_url,
        reference=f"reconnect-{connection_id}",
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    # Update connection with new requisition
    conn.gc_requisition_id = result["requisition_id"]
    conn.gc_agreement_id = result.get("agreement_id") or conn.gc_agreement_id
    conn.encrypted_creds = (
        result.get("updated_credentials") or conn.encrypted_creds
    )
    conn.status = "connecting"
    conn.updated_at = _utcnow_str()

    return ApiSuccess(
        data=ConnectionCreateResult(
            requisition_id=result["requisition_id"],
            link=result["link"],
            status=result.get("status", "CR"),
            agreement_id=result.get("agreement_id"),
        )
    )


# ─── Status ───────────────────────────────────────────────────────────────


@router.get("/status", response_model=ApiSuccess[BankSyncStatusOut])
async def get_sync_status(
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_auth),
):
    """Get overall bank sync service status."""
    # Count connections
    total_stmt = (
        select(func.count())
        .select_from(BankConnection)
        .where(BankConnection.deleted_at.is_(None))
    )
    active_stmt = (
        select(func.count())
        .select_from(BankConnection)
        .where(
            BankConnection.deleted_at.is_(None),
            BankConnection.status == "active",
        )
    )
    last_sync_stmt = (
        select(BankConnection.last_sync_at)
        .where(
            BankConnection.deleted_at.is_(None),
            BankConnection.last_sync_at.is_(None),
        )
        .order_by(BankConnection.last_sync_at.desc())
        .limit(1)
    )

    total = (await db.execute(total_stmt)).scalar() or 0
    active = (await db.execute(active_stmt)).scalar() or 0

    # Find the most recent sync
    recent_stmt = (
        select(BankConnection.last_sync_at, BankConnection.next_sync_at)
        .where(
            BankConnection.deleted_at.is_(None),
            BankConnection.last_sync_at.isnot(None),
        )
        .order_by(BankConnection.last_sync_at.desc())
        .limit(1)
    )
    recent = (await db.execute(recent_stmt)).first()

    return ApiSuccess(
        data=BankSyncStatusOut(
            configured=bool(os.environ.get("FINANCE_BANK_ENCRYPTION_KEY")),
            provider="gocardless" if active > 0 else None,
            active_connections=active,
            total_connections=total,
            last_global_sync=recent[0] if recent else None,
            next_scheduled_sync=recent[1] if recent else None,
            encryption_key_set=bool(os.environ.get("FINANCE_BANK_ENCRYPTION_KEY")),
        )
    )


# ─── Providers ────────────────────────────────────────────────────────────


@router.get("/providers", response_model=ApiSuccess[list[dict]])
async def list_providers(
    _auth: None = Depends(require_auth),
):
    """List supported bank data providers."""
    return ApiSuccess(
        data=[
            {
                "id": "gocardless",
                "name": "GoCardless Bank Account Data",
                "description": "Free PSD2 Open Banking API. Covers 2000+ European banks.",
                "status": "available",
                "countries": "EU/EEA + UK",
                "cost": "free",
            }
        ]
    )
