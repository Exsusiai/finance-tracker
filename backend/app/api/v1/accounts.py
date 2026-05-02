"""Account routes — CRUD + balance queries."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.errors import NotFoundError
from app.db import get_db
from app.models import Account
from app.models import touch_updated_at
from app.schemas import (
    AccountCreate,
    AccountOut,
    AccountUpdate,
    ApiSuccess,
    BalanceOut,
    PaginationMeta,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


def _normalize_amount(val) -> str:
    """Normalize Decimal amount to clean string representation."""
    if val is None:
        return "0"
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    # normalize() can produce scientific notation (1E+3), so format manually
    n = d.normalize()
    sign, digits, exponent = n.as_tuple()
    if exponent >= 0:
        # Integer: no decimal point needed
        return str(int(n))
    # Has decimal part
    return format(n, 'f')


def _account_to_out(a: Account) -> AccountOut:
    return AccountOut(
        id=a.id,
        name=a.name,
        type=a.type,
        institution=a.institution,
        account_number=a.account_number,
        currency=a.currency,
        initial_balance=_normalize_amount(a.initial_balance),
        is_active=a.is_active,
        notes=a.notes,
        metadata_json=a.metadata_json,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@router.get("", response_model=ApiSuccess[list[AccountOut]])
async def list_accounts(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(False),
):
    stmt = select(Account).where(Account.deleted_at.is_(None))
    if active_only:
        stmt = stmt.where(Account.is_active.is_(True))
    stmt = stmt.order_by(Account.id)
    result = await db.execute(stmt)
    accounts = result.scalars().all()
    return ApiSuccess(data=[_account_to_out(a) for a in accounts])


@router.post("", response_model=ApiSuccess[AccountOut], status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    account = Account(
        name=body.name,
        type=body.type,
        institution=body.institution,
        account_number=body.account_number,
        currency=body.currency,
        initial_balance=Decimal(body.initial_balance),
        notes=body.notes,
        metadata_json=body.metadata_json,
    )
    db.add(account)
    await db.flush()
    return ApiSuccess(data=_account_to_out(account))


@router.get("/balances", response_model=ApiSuccess[list[BalanceOut]])
async def list_all_balances(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Return current balance for all active accounts (from v_account_balance view)."""
    stmt = text("""
        SELECT account_id, account_name, currency, balance
        FROM v_account_balance
    """)
    result = await db.execute(stmt)
    rows = result.all()
    balances = [
        BalanceOut(account_id=r[0], account_name=r[1], currency=r[2], balance=_normalize_amount(r[3]))
        for r in rows
    ]
    return ApiSuccess(data=balances)


@router.get("/{account_id}", response_model=ApiSuccess[AccountOut])
async def get_account(
    account_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    if not account:
        raise NotFoundError("Account", account_id)
    return ApiSuccess(data=_account_to_out(account))


@router.get("/{account_id}/balance", response_model=ApiSuccess[BalanceOut])
async def get_account_balance(
    account_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = text("""
        SELECT account_id, account_name, currency, balance
        FROM v_account_balance
        WHERE account_id = :aid
    """)
    result = await db.execute(stmt, {"aid": account_id})
    row = result.first()
    if not row:
        raise NotFoundError("Account", account_id)
    return ApiSuccess(data=BalanceOut(
        account_id=row[0], account_name=row[1], currency=row[2], balance=_normalize_amount(row[3])
    ))


@router.patch("/{account_id}", response_model=ApiSuccess[AccountOut])
async def update_account(
    account_id: int,
    body: AccountUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    if not account:
        raise NotFoundError("Account", account_id)

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if key == "initial_balance":
            value = Decimal(value)
        setattr(account, key, value)

    touch_updated_at(account)
    await db.flush()
    return ApiSuccess(data=_account_to_out(account))


@router.delete("/{account_id}", response_model=ApiSuccess[dict])
async def delete_account(
    account_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    if not account:
        raise NotFoundError("Account", account_id)

    from datetime import datetime, timezone
    account.deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.flush()
    return ApiSuccess(data={"id": account_id, "deleted": True})
