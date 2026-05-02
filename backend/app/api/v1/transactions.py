"""Transaction routes — CRUD + batch + filtering."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import require_auth
from app.core.errors import NotFoundError
from app.db import get_db
from app.models import Transaction, Account, Category
from app.models import touch_updated_at
from app.schemas import (
    ApiSuccess,
    PaginationMeta,
    TransactionBatchCreate,
    TransactionCreate,
    TransactionOut,
    TransactionUpdate,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


def _normalize_amount(val) -> str:
    """Normalize Decimal to clean string (no trailing zeros, no scientific notation)."""
    if val is None:
        return "0"
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    n = d.normalize()
    sign, digits, exponent = n.as_tuple()
    if exponent >= 0:
        return str(int(n))
    return format(n, 'f')


def _tx_to_out(t: Transaction) -> TransactionOut:
    tags = []
    if t.tags_json:
        try:
            tags = json.loads(t.tags_json)
        except (json.JSONDecodeError, TypeError):
            tags = []
    return TransactionOut(
        id=t.id,
        account_id=t.account_id,
        account_name=t.account.name if t.account else None,
        counter_account_id=t.counter_account_id,
        category_id=t.category_id,
        category_name=t.category.name if t.category else None,
        occurred_at=t.occurred_at,
        posted_at=t.posted_at,
        amount=_normalize_amount(t.amount),
        currency=t.currency,
        fx_rate_to_base=_normalize_amount(t.fx_rate_to_base) if t.fx_rate_to_base else None,
        base_amount=_normalize_amount(t.base_amount) if t.base_amount else None,
        type=t.type,
        description=t.description,
        raw_description=t.raw_description,
        counterparty=t.counterparty,
        location=t.location,
        tags=tags,
        source=t.source,
        pdf_import_id=t.pdf_import_id,
        external_id=t.external_id,
        is_pending=t.is_pending,
        metadata_json=t.metadata_json,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.get("", response_model=ApiSuccess[list[TransactionOut]])
async def list_transactions(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    account_id: int | None = Query(None),
    category_id: int | None = Query(None),
    type: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    min_amount: str | None = Query(None),
    max_amount: str | None = Query(None),
    search: str | None = Query(None),
    tags: str | None = Query(None),  # CSV
    source: str | None = Query(None),
    is_pending: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: int | None = Query(None),
):
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.deleted_at.is_(None))
        .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
    )

    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    if category_id is not None:
        stmt = stmt.where(Transaction.category_id == category_id)
    if type is not None:
        stmt = stmt.where(Transaction.type == type)
    if from_date is not None:
        stmt = stmt.where(Transaction.occurred_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(Transaction.occurred_at < to_date + "T23:59:59Z")
    if min_amount is not None:
        stmt = stmt.where(Transaction.amount >= Decimal(min_amount))
    if max_amount is not None:
        stmt = stmt.where(Transaction.amount <= Decimal(max_amount))
    if search is not None:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Transaction.description.ilike(pattern),
                Transaction.counterparty.ilike(pattern),
                Transaction.raw_description.ilike(pattern),
            )
        )
    if tags is not None:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        for tag in tag_list:
            stmt = stmt.where(Transaction.tags_json.contains(tag))
    if source is not None:
        stmt = stmt.where(Transaction.source == source)
    if is_pending is not None:
        stmt = stmt.where(Transaction.is_pending == (1 if is_pending else 0))

    # Cursor-based pagination
    if cursor is not None:
        stmt = stmt.where(Transaction.id < cursor)

    stmt = stmt.limit(limit + 1)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    # Count total
    count_stmt = (
        select(func.count(Transaction.id))
        .where(Transaction.deleted_at.is_(None))
    )
    # Apply same filters for count
    if account_id is not None:
        count_stmt = count_stmt.where(Transaction.account_id == account_id)
    if category_id is not None:
        count_stmt = count_stmt.where(Transaction.category_id == category_id)
    if type is not None:
        count_stmt = count_stmt.where(Transaction.type == type)
    if from_date is not None:
        count_stmt = count_stmt.where(Transaction.occurred_at >= from_date)
    if to_date is not None:
        count_stmt = count_stmt.where(Transaction.occurred_at < to_date + "T23:59:59Z")

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    next_cursor = str(rows[-1].id) if has_more and rows else None
    meta = PaginationMeta(next_cursor=next_cursor, total=total)

    return ApiSuccess(
        data=[_tx_to_out(t) for t in rows],
        meta=meta.model_dump(),
    )


@router.post("", response_model=ApiSuccess[TransactionOut], status_code=status.HTTP_201_CREATED)
async def create_transaction(
    body: TransactionCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    tx = Transaction(
        account_id=body.account_id,
        counter_account_id=body.counter_account_id,
        category_id=body.category_id,
        occurred_at=body.occurred_at,
        posted_at=body.posted_at,
        amount=Decimal(body.amount),
        currency=body.currency,
        fx_rate_to_base=Decimal(body.fx_rate_to_base) if body.fx_rate_to_base else None,
        base_amount=Decimal(body.base_amount) if body.base_amount else None,
        type=body.type,
        description=body.description,
        raw_description=body.raw_description,
        counterparty=body.counterparty,
        location=body.location,
        tags_json=json.dumps(body.tags) if body.tags else None,
        source=body.source,
        external_id=body.external_id,
        is_pending=body.is_pending,
        metadata_json=body.metadata_json,
    )
    db.add(tx)
    await db.flush()
    # Re-fetch with relationships loaded
    fetch = select(Transaction).options(selectinload(Transaction.account), selectinload(Transaction.category)).where(Transaction.id == tx.id)
    result = await db.execute(fetch)
    tx = result.scalar_one()
    return ApiSuccess(data=_tx_to_out(tx))


@router.post("/batch", response_model=ApiSuccess[list[TransactionOut]], status_code=status.HTTP_201_CREATED)
async def batch_create_transactions(
    body: TransactionBatchCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    created = []
    for t_data in body.transactions:
        tx = Transaction(
            account_id=t_data.account_id,
            counter_account_id=t_data.counter_account_id,
            category_id=t_data.category_id,
            occurred_at=t_data.occurred_at,
            posted_at=t_data.posted_at,
            amount=Decimal(t_data.amount),
            currency=t_data.currency,
            fx_rate_to_base=Decimal(t_data.fx_rate_to_base) if t_data.fx_rate_to_base else None,
            base_amount=Decimal(t_data.base_amount) if t_data.base_amount else None,
            type=t_data.type,
            description=t_data.description,
            raw_description=t_data.raw_description,
            counterparty=t_data.counterparty,
            location=t_data.location,
            tags_json=json.dumps(t_data.tags) if t_data.tags else None,
            source=t_data.source,
            external_id=t_data.external_id,
            is_pending=t_data.is_pending,
            metadata_json=t_data.metadata_json,
        )
        db.add(tx)
        created.append(tx)
    await db.flush()
    # Batch re-fetch with relationships
    if created:
        ids = [tx.id for tx in created]
        fetch = (
            select(Transaction)
            .options(selectinload(Transaction.account), selectinload(Transaction.category))
            .where(Transaction.id.in_(ids))
        )
        result = await db.execute(fetch)
        created = list(result.scalars().all())
    return ApiSuccess(data=[_tx_to_out(tx) for tx in created])


@router.get("/{transaction_id}", response_model=ApiSuccess[TransactionOut])
async def get_transaction(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    result = await db.execute(stmt)
    tx = result.scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)
    return ApiSuccess(data=_tx_to_out(tx))


@router.patch("/{transaction_id}", response_model=ApiSuccess[TransactionOut])
async def update_transaction(
    transaction_id: int,
    body: TransactionUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    result = await db.execute(stmt)
    tx = result.scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    update_data = body.model_dump(exclude_unset=True)
    if "tags" in update_data:
        update_data["tags_json"] = json.dumps(update_data.pop("tags")) if update_data["tags"] else None
    if "amount" in update_data and update_data["amount"] is not None:
        update_data["amount"] = Decimal(update_data["amount"])
    if "fx_rate_to_base" in update_data and update_data["fx_rate_to_base"] is not None:
        update_data["fx_rate_to_base"] = Decimal(update_data["fx_rate_to_base"])
    if "base_amount" in update_data and update_data["base_amount"] is not None:
        update_data["base_amount"] = Decimal(update_data["base_amount"])

    for key, value in update_data.items():
        setattr(tx, key, value)

    touch_updated_at(tx)
    await db.flush()
    return ApiSuccess(data=_tx_to_out(tx))


@router.delete("/{transaction_id}", response_model=ApiSuccess[dict])
async def delete_transaction(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    result = await db.execute(stmt)
    tx = result.scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    tx.deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.flush()
    return ApiSuccess(data={"id": transaction_id, "deleted": True})


@router.post("/{transaction_id}/categorize", response_model=ApiSuccess[TransactionOut])
async def recategorize_transaction(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Re-run categorization rules on a single transaction."""
    from app.services.categorizer.engine import categorize_transaction

    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    result = await db.execute(stmt)
    tx = result.scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    await categorize_transaction(db, tx)
    await db.flush()
    return ApiSuccess(data=_tx_to_out(tx))
