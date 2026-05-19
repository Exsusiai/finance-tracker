"""P1-4 A4.3: crypto wallet + CEX endpoints.

All routes live under ``/accounts/{account_id}/…`` so they sit next to
the existing accounts CRUD in the OpenAPI spec.

Secrets are write-only. ``ExchangeConnectionOut`` returns boolean
``has_credentials`` / ``has_passphrase`` flags but never the key,
secret, or passphrase themselves. Rotation = PUT a fresh
``ExchangeConnectionIn`` (replaces the row).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.config import get_settings
from app.db import get_db
from app.models import (
    Account,
    ChainAddress,
    ExchangeConnection,
    _utcnow_str,
    touch_updated_at,
)
from app.schemas import (
    ApiSuccess,
    ChainAddressIn,
    ChainAddressOut,
    ExchangeConnectionIn,
    ExchangeConnectionOut,
    SyncResultOut,
    SyncSummaryOut,
)
from app.services.bank_sync.crypto import encrypt_str
from app.services.wallet_sync import sync_account

router = APIRouter(dependencies=[Depends(require_auth)])
_db_dep = Annotated[AsyncSession, Depends(get_db)]


# ─── helpers ───────────────────────────────────────────────────────────────


async def _get_account(db: AsyncSession, account_id: int, want_type: str) -> Account:
    acc = (
        await db.execute(
            select(Account).where(
                Account.id == account_id,
                Account.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Account {account_id} not found")
    if acc.type != want_type:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Account {account_id} is type {acc.type!r}; this endpoint needs {want_type!r}",
        )
    return acc


# ─── Chain addresses (crypto_wallet) ───────────────────────────────────────


@router.get(
    "/accounts/{account_id}/addresses",
    response_model=ApiSuccess[list[ChainAddressOut]],
)
async def list_addresses(account_id: int, db: _db_dep):
    await _get_account(db, account_id, "crypto_wallet")
    rows = (
        await db.execute(
            select(ChainAddress)
            .where(ChainAddress.account_id == account_id)
            .order_by(ChainAddress.id)
        )
    ).scalars().all()
    return ApiSuccess(data=[ChainAddressOut.model_validate(r) for r in rows])


@router.post(
    "/accounts/{account_id}/addresses",
    response_model=ApiSuccess[ChainAddressOut],
    status_code=status.HTTP_201_CREATED,
)
async def add_address(account_id: int, body: ChainAddressIn, db: _db_dep):
    await _get_account(db, account_id, "crypto_wallet")
    row = ChainAddress(
        account_id=account_id,
        chain=body.chain.strip().lower(),
        address=body.address.strip(),
        label=body.label,
        created_at=_utcnow_str(),
        updated_at=_utcnow_str(),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Specific case we know about: unique violation on
        # (account, chain, address). Everything else (DB timeout,
        # connection errors, etc.) keeps propagating as 5xx so the
        # client doesn't see a misleading 409.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Address already registered on this chain for this account",
        ) from exc
    return ApiSuccess(data=ChainAddressOut.model_validate(row))


@router.delete(
    "/accounts/{account_id}/addresses/{addr_id}",
    response_model=ApiSuccess[dict],
)
async def delete_address(account_id: int, addr_id: int, db: _db_dep):
    await _get_account(db, account_id, "crypto_wallet")
    row = (
        await db.execute(
            select(ChainAddress).where(
                ChainAddress.id == addr_id,
                ChainAddress.account_id == account_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Address not found")
    await db.delete(row)
    return ApiSuccess(data={"deleted": addr_id})


# ─── Exchange connection (exchange) ────────────────────────────────────────


def _conn_to_out(row: ExchangeConnection) -> ExchangeConnectionOut:
    return ExchangeConnectionOut(
        id=row.id,
        exchange=row.exchange,
        has_credentials=bool(row.api_key_enc and row.api_secret_enc),
        has_passphrase=bool(row.api_passphrase_enc),
        last_synced_at=row.last_synced_at,
        last_sync_status=row.last_sync_status,
        last_sync_error=row.last_sync_error,
    )


@router.get(
    "/accounts/{account_id}/exchange-connection",
    response_model=ApiSuccess[ExchangeConnectionOut | None],
)
async def get_exchange_connection(account_id: int, db: _db_dep):
    await _get_account(db, account_id, "exchange")
    row = (
        await db.execute(
            select(ExchangeConnection).where(ExchangeConnection.account_id == account_id)
        )
    ).scalar_one_or_none()
    return ApiSuccess(data=_conn_to_out(row) if row else None)


@router.put(
    "/accounts/{account_id}/exchange-connection",
    response_model=ApiSuccess[ExchangeConnectionOut],
)
async def upsert_exchange_connection(
    account_id: int, body: ExchangeConnectionIn, db: _db_dep
):
    await _get_account(db, account_id, "exchange")
    exchange = body.exchange.strip().lower()
    if exchange not in ("binance", "bitget"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unsupported exchange {body.exchange!r}"
        )
    if exchange == "bitget" and not body.passphrase:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Bitget requires a passphrase (set at API-key creation).",
        )

    existing = (
        await db.execute(
            select(ExchangeConnection).where(ExchangeConnection.account_id == account_id)
        )
    ).scalar_one_or_none()

    now = _utcnow_str()
    key_enc = encrypt_str(body.api_key)
    secret_enc = encrypt_str(body.api_secret)
    pp_enc = encrypt_str(body.passphrase) if body.passphrase else None

    if existing is None:
        row = ExchangeConnection(
            account_id=account_id,
            exchange=exchange,
            api_key_enc=key_enc,
            api_secret_enc=secret_enc,
            api_passphrase_enc=pp_enc,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row = existing
        row.exchange = exchange
        row.api_key_enc = key_enc
        row.api_secret_enc = secret_enc
        row.api_passphrase_enc = pp_enc
        # Reset sync state on credential rotation — the previous error
        # almost certainly refers to the old key.
        row.last_sync_status = None
        row.last_sync_error = None
        touch_updated_at(row)
    await db.flush()
    return ApiSuccess(data=_conn_to_out(row))


@router.delete(
    "/accounts/{account_id}/exchange-connection",
    response_model=ApiSuccess[dict],
)
async def delete_exchange_connection(account_id: int, db: _db_dep):
    await _get_account(db, account_id, "exchange")
    row = (
        await db.execute(
            select(ExchangeConnection).where(ExchangeConnection.account_id == account_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No connection to delete")
    await db.delete(row)
    return ApiSuccess(data={"deleted": row.id})


# ─── Sync (blocking) ───────────────────────────────────────────────────────


@router.post(
    "/accounts/{account_id}/sync",
    response_model=ApiSuccess[SyncSummaryOut],
)
async def sync(account_id: int, db: _db_dep):
    """Trigger a blocking on-chain / CEX balance sync for one account.

    Sync runs in-band — per the user's decision (2026-05-18) a personal
    wallet's 1-3 addresses finish in a few seconds, so we skip the job
    queue. The frontend should show a loading state while this is in
    flight.
    """
    settings = get_settings()
    try:
        summary = await sync_account(
            db, account_id, alchemy_api_key=settings.alchemy_api_key or None
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return ApiSuccess(
        data=SyncSummaryOut(
            account_id=summary.account_id,
            account_type=summary.account_type,
            total_synced=summary.total_synced,
            total_errors=summary.total_errors,
            results=[SyncResultOut(**r.__dict__) for r in summary.results],
        )
    )
