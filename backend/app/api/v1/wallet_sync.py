"""P1-4 A4.3: crypto wallet + CEX endpoints.

All routes live under ``/accounts/{account_id}/…`` so they sit next to
the existing accounts CRUD in the OpenAPI spec.

Secrets are write-only. ``ExchangeConnectionOut`` returns boolean
``has_credentials`` / ``has_passphrase`` flags but never the key,
secret, or passphrase themselves. Rotation = PUT a fresh
``ExchangeConnectionIn`` (replaces the row).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.config import get_settings
from app.db import get_db
from app.models import (
    Account,
    BrokerConnection,
    ChainAddress,
    ExchangeConnection,
    _utcnow_str,
    touch_updated_at,
)
from app.schemas import (
    ApiSuccess,
    BrokerConnectionIn,
    BrokerConnectionOut,
    ChainAddressIn,
    ChainAddressOut,
    ExchangeConnectionIn,
    ExchangeConnectionOut,
    SyncResultOut,
    SyncSummaryOut,
    TRConnectIn,
    TRConnectOut,
    TRVerifyIn,
)
from app.services.bank_sync.crypto import encrypt_str
from app.services.wallet_sync import sync_account

router = APIRouter(dependencies=[Depends(require_auth)])
_db_dep = Annotated[AsyncSession, Depends(get_db)]
log = structlog.get_logger(__name__)


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
    from app.services.bank_sync.crypto import can_decrypt

    has_creds = bool(row.api_key_enc and row.api_secret_enc)
    # Stale = rows present but undecryptable under the current encryption
    # key (rotated). Surfaced so the UI prompts re-entry instead of letting
    # sync fail at decrypt time (ERR-20260607-001).
    stale = has_creds and not (
        can_decrypt(row.api_key_enc) and can_decrypt(row.api_secret_enc)
    )
    return ExchangeConnectionOut(
        id=row.id,
        exchange=row.exchange,
        has_credentials=has_creds,
        has_passphrase=bool(row.api_passphrase_enc),
        credentials_stale=stale,
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
    # V5-P2-3 / 2026-05-20: surface missing FINANCE_BANK_ENCRYPTION_KEY as a
    # 400 configuration error instead of a generic 500. The user can fix
    # this by editing .env (see .env.example) — telling them so directly
    # is much better than a stack trace in the browser console.
    try:
        key_enc = encrypt_str(body.api_key)
        secret_enc = encrypt_str(body.api_secret)
        pp_enc = encrypt_str(body.passphrase) if body.passphrase else None
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Backend not configured for encrypted credentials: "
            "set FINANCE_BANK_ENCRYPTION_KEY in .env (see .env.example), then restart.",
        ) from exc

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


# ─── Broker connection (brokerage) ─────────────────────────────────────────


_SUPPORTED_BROKERS = ("ibkr",)


def _broker_conn_to_out(row: BrokerConnection) -> BrokerConnectionOut:
    from app.services.bank_sync.crypto import can_decrypt

    has_token = bool(row.token_enc)
    stale = has_token and not can_decrypt(row.token_enc)
    return BrokerConnectionOut(
        id=row.id,
        provider=row.provider,
        query_id=row.query_id,
        has_token=has_token,
        credentials_stale=stale,
        last_synced_at=row.last_synced_at,
        last_sync_status=row.last_sync_status,
        last_sync_error=row.last_sync_error,
    )


@router.get(
    "/accounts/{account_id}/broker-connection",
    response_model=ApiSuccess[BrokerConnectionOut | None],
)
async def get_broker_connection(account_id: int, db: _db_dep):
    await _get_account(db, account_id, "brokerage")
    row = (
        await db.execute(
            select(BrokerConnection).where(BrokerConnection.account_id == account_id)
        )
    ).scalar_one_or_none()
    return ApiSuccess(data=_broker_conn_to_out(row) if row else None)


@router.put(
    "/accounts/{account_id}/broker-connection",
    response_model=ApiSuccess[BrokerConnectionOut],
)
async def upsert_broker_connection(
    account_id: int, body: BrokerConnectionIn, db: _db_dep
):
    await _get_account(db, account_id, "brokerage")
    provider = body.provider.strip().lower()
    if provider not in _SUPPORTED_BROKERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unsupported broker {body.provider!r}"
        )

    existing = (
        await db.execute(
            select(BrokerConnection).where(BrokerConnection.account_id == account_id)
        )
    ).scalar_one_or_none()

    now = _utcnow_str()
    try:
        token_enc = encrypt_str(body.token)
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Backend not configured for encrypted credentials: "
            "set FINANCE_BANK_ENCRYPTION_KEY in .env (see .env.example), then restart.",
        ) from exc

    if existing is None:
        row = BrokerConnection(
            account_id=account_id,
            provider=provider,
            token_enc=token_enc,
            query_id=body.query_id.strip(),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row = existing
        row.provider = provider
        row.token_enc = token_enc
        row.query_id = body.query_id.strip()
        # Reset sync state on rotation — a prior error refers to the old token.
        row.last_sync_status = None
        row.last_sync_error = None
        touch_updated_at(row)
    await db.flush()
    return ApiSuccess(data=_broker_conn_to_out(row))


@router.delete(
    "/accounts/{account_id}/broker-connection",
    response_model=ApiSuccess[dict],
)
async def delete_broker_connection(account_id: int, db: _db_dep):
    await _get_account(db, account_id, "brokerage")
    row = (
        await db.execute(
            select(BrokerConnection).where(BrokerConnection.account_id == account_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No connection to delete")
    await db.delete(row)
    return ApiSuccess(data={"deleted": row.id})


# ─── Trade Republic: 2-step interactive login ──────────────────────────────
#
# TR has no static token — login is phone+PIN → 4-digit code → cookie
# session. The live pytr object from step 1 must survive until step 2, so we
# hold it in a process-local store keyed by account_id. This is safe for the
# local-first single-process backend; a restart mid-login just means the user
# re-initiates (cheap). Entries auto-expire.

_TR_PENDING: dict[int, dict] = {}
_TR_PENDING_GRACE_SEC = 120  # extra time past the TR countdown to enter the code


def _tr_prune_expired() -> None:
    now = time.monotonic()
    for aid in [a for a, v in _TR_PENDING.items() if v["expires_at"] < now]:
        from app.services.broker_sync.traderepublic import cleanup_login

        try:
            cleanup_login(_TR_PENDING[aid]["tr"])
        except Exception:  # noqa: BLE001
            pass
        _TR_PENDING.pop(aid, None)


def _mask_phone(phone: str) -> str:
    p = phone.strip()
    if len(p) <= 5:
        return p
    return f"{p[:3]}•••{p[-2:]}"


@router.post(
    "/accounts/{account_id}/broker-connection/tr/connect",
    response_model=ApiSuccess[TRConnectOut],
)
async def tr_connect(account_id: int, body: TRConnectIn, db: _db_dep):
    """Trade Republic login step 1: phone + PIN → TR sends a 4-digit code."""
    await _get_account(db, account_id, "brokerage")
    _tr_prune_expired()

    from app.services.broker_sync import BrokerSyncError
    from app.services.broker_sync.traderepublic import cleanup_login, initiate_login

    # Drop any previous half-finished attempt for this account.
    prev = _TR_PENDING.pop(account_id, None)
    if prev:
        try:
            cleanup_login(prev["tr"])
        except Exception:  # noqa: BLE001
            pass

    try:
        # initiate_weblogin is blocking (playwright/curl_cffi WAF + HTTP) — off the loop.
        tr, countdown = await asyncio.to_thread(
            initiate_login, body.phone.strip(), body.pin.strip()
        )
    except BrokerSyncError as exc:
        msg = str(exc)
        # Full reason to the server log (so we can debug login failures);
        # the message is already user-safe (no secrets).
        log.warning("tr_connect_failed", account_id=account_id, reason=msg)
        # Credential / validation problems are the caller's fault → 400;
        # WAF / upstream issues → 502.
        is_user_error = any(
            k in msg for k in ("手机号", "PIN", "频繁", "验证码")
        )
        code = status.HTTP_400_BAD_REQUEST if is_user_error else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, msg) from exc
    except Exception as exc:  # noqa: BLE001 — surface unexpected errors, don't 500 silently
        log.warning(
            "tr_connect_unexpected",
            account_id=account_id,
            error_class=exc.__class__.__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Trade Republic 连接异常：{exc.__class__.__name__}: {str(exc)[:200]}",
        ) from exc

    _TR_PENDING[account_id] = {
        "tr": tr,
        "phone": body.phone.strip(),
        "expires_at": time.monotonic() + countdown + _TR_PENDING_GRACE_SEC,
    }
    return ApiSuccess(data=TRConnectOut(countdown_seconds=countdown))


@router.post(
    "/accounts/{account_id}/broker-connection/tr/verify",
    response_model=ApiSuccess[BrokerConnectionOut],
)
async def tr_verify(account_id: int, body: TRVerifyIn, db: _db_dep):
    """Trade Republic login step 2: submit the 4-digit code → store session."""
    await _get_account(db, account_id, "brokerage")
    _tr_prune_expired()

    from app.services.broker_sync import BrokerSyncError
    from app.services.broker_sync.traderepublic import cleanup_login, complete_login

    pending = _TR_PENDING.get(account_id)
    if pending is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "登录会话已过期或未发起，请重新点击连接。",
        )

    try:
        cookies_blob = await asyncio.to_thread(complete_login, pending["tr"], body.code)
    except BrokerSyncError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        cleanup_login(pending["tr"])
        _TR_PENDING.pop(account_id, None)

    try:
        token_enc = encrypt_str(cookies_blob)
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Backend not configured for encrypted credentials: "
            "set FINANCE_BANK_ENCRYPTION_KEY in .env (see .env.example), then restart.",
        ) from exc

    now = _utcnow_str()
    meta = json.dumps({"phone_masked": _mask_phone(pending["phone"])})
    existing = (
        await db.execute(
            select(BrokerConnection).where(BrokerConnection.account_id == account_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        row = BrokerConnection(
            account_id=account_id,
            provider="traderepublic",
            token_enc=token_enc,
            query_id=None,
            metadata_json=meta,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row = existing
        row.provider = "traderepublic"
        row.token_enc = token_enc
        row.query_id = None
        row.metadata_json = meta
        row.last_sync_status = None
        row.last_sync_error = None
        touch_updated_at(row)
    await db.flush()
    return ApiSuccess(data=_broker_conn_to_out(row))


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
