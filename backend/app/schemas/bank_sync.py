"""Pydantic v2 schemas for bank sync API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ─── GoCardless Setup ─────────────────────────────────────────────────────

class GoCardlessSetupRequest(BaseModel):
    """Configure GoCardless API credentials."""

    secret_id: str = Field(min_length=1, description="GoCardless secret ID")
    secret_key: str = Field(min_length=1, description="GoCardless secret key")


class GoCardlessSetupResponse(BaseModel):
    setup_ok: bool
    has_access_token: bool = False
    has_refresh_token: bool = False
    encrypted_credentials: str | None = None
    error: str | None = None


# ─── Institution ──────────────────────────────────────────────────────────

class InstitutionListRequest(BaseModel):
    """Body for listing institutions.

    V7-P1-7: the encrypted GoCardless credential blob is sent in the POST
    body, not the query string — a query string lands in browser history,
    proxy/access logs, APM traces and crash reports, where (even encrypted)
    it is a replayable credential for this service.
    """

    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")
    encrypted_credentials: str = Field(min_length=1, description="Encrypted GoCardless credentials")


class InstitutionOut(BaseModel):
    id: str
    name: str
    bic: str | None = None
    country: str | None = None
    logo_url: str | None = None
    transaction_total_days: int = 90
    max_access_valid_for_days: int = 90


# ─── Bank Connection ─────────────────────────────────────────────────────

class BankConnectionCreate(BaseModel):
    provider: str = Field(pattern=r"^(gocardless)$")
    encrypted_credentials: str
    institution_id: str
    # V7-P1-7: explicit country for the institution lookup. Previously the
    # route passed `redirect_url` as the country, which broke the lookup and
    # could surface wrong institution metadata.
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")
    redirect_url: str = Field(
        default="http://localhost:3000/settings/bank-sync/callback",
        description="URL to redirect after bank auth",
    )
    reference: str | None = None
    max_historical_days: int = Field(default=540, ge=1, le=730)


class BankConnectionUpdate(BaseModel):
    account_id: int | None = None
    sync_interval_hours: int | None = Field(default=None, ge=1, le=168)
    encrypted_credentials: str | None = None


class BankConnectionOut(BaseModel):
    id: int
    provider: str
    institution_id: str
    institution_name: str
    institution_bic: str | None
    institution_country: str
    institution_logo: str | None
    gc_requisition_id: str | None
    gc_agreement_id: str | None
    gc_account_ids_json: str | None
    account_id: int | None
    currency: str
    status: str
    last_sync_at: str | None
    last_sync_status: str | None
    last_sync_error: str | None
    next_sync_at: str | None
    sync_interval_hours: int
    total_transactions: int
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# ─── Sync Result ──────────────────────────────────────────────────────────

class SyncResultOut(BaseModel):
    success: bool
    transactions_new: int = 0
    transactions_existing: int = 0
    transactions_pending: int = 0
    balance_amount: str | None = None
    balance_currency: str | None = None
    error: str | None = None
    next_sync_at: str | None = None
    rate_limit_remaining: int | None = None


# ─── Callback ─────────────────────────────────────────────────────────────

class CallbackResultOut(BaseModel):
    status: str
    linked: bool
    accounts: list[dict[str, Any]] = []
    agreement_id: str | None = None
    institution_id: str | None = None
    error: str | None = None


# ─── Connection Create Result ─────────────────────────────────────────────

class ConnectionCreateResult(BaseModel):
    requisition_id: str
    link: str
    status: str
    agreement_id: str | None = None
    updated_credentials: str | None = None


# ─── Service Status ───────────────────────────────────────────────────────

class BankSyncStatusOut(BaseModel):
    configured: bool
    provider: str | None = None
    active_connections: int = 0
    total_connections: int = 0
    last_global_sync: str | None = None
    next_scheduled_sync: str | None = None
    encryption_key_set: bool = False
