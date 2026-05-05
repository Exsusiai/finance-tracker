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
from app.services.cashflow import parse_period, recompute_for_periods, recompute_period

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
        user_note=t.user_note,
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
    limit: int = Query(50, ge=1, le=1000),
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
    # Auto-refresh cashflow snapshot for the affected period
    period = parse_period(tx.occurred_at)
    if period:
        await recompute_period(db, period[0], period[1])
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
    # Auto-refresh cashflow snapshots for all affected periods (deduplicated)
    await recompute_for_periods(db, [parse_period(t.occurred_at) for t in created])
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

    old_period = parse_period(tx.occurred_at)
    old_category_id = tx.category_id

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

    # Auto-learn: if user changed the category, derive (or strengthen) a rule
    new_category_id = tx.category_id
    if (
        new_category_id is not None
        and new_category_id != old_category_id
        and tx.source != "manual"  # only learn from imported tx where description is "real" merchant text
    ):
        from app.services.categorizer.engine import learn_from_user_assignment
        await learn_from_user_assignment(db, tx, new_category_id)

    # Recompute both old and new period (occurred_at may have shifted across months)
    new_period = parse_period(tx.occurred_at)
    await recompute_for_periods(db, [old_period, new_period])
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
    period = parse_period(tx.occurred_at)
    await db.flush()
    if period:
        await recompute_period(db, period[0], period[1])
    return ApiSuccess(data={"id": transaction_id, "deleted": True})


# ─── Transfer matching ────────────────────────────────────────────────


@router.post("/{transaction_id}/mark-transfer", response_model=ApiSuccess[TransactionOut])
async def mark_as_transfer(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    counter_transaction_id: int | None = Query(None,
        description="If known, the matching tx in the other account. Both rows get type='transfer' and cross-link."),
):
    """Mark a single tx as a transfer; optionally pair with a counter-leg.

    Without `counter_transaction_id`: just flips type → 'transfer' (useful when
    only one side of the transfer was recorded, e.g. a one-off SEPA).
    With `counter_transaction_id`: also flips that side and cross-links the two.
    """
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    tx = (await db.execute(stmt)).scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    old_period = parse_period(tx.occurred_at)
    tx.type = "transfer"
    tx.is_pending = False

    if counter_transaction_id is not None:
        ctr = (await db.execute(
            select(Transaction).where(
                Transaction.id == counter_transaction_id,
                Transaction.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not ctr:
            raise NotFoundError("Counter Transaction", counter_transaction_id)
        ctr.type = "transfer"
        ctr.is_pending = False
        tx.counter_account_id = ctr.account_id
        ctr.counter_account_id = tx.account_id
        # Recompute both periods (counter may be on a different month)
        ctr_period = parse_period(ctr.occurred_at)
        await db.flush()
        await recompute_for_periods(db, [old_period, ctr_period])
    else:
        await db.flush()
        await recompute_for_periods(db, [old_period])

    return ApiSuccess(data=_tx_to_out(tx))


@router.get("/transfers/suggestions", response_model=ApiSuccess[list[dict]])
async def get_transfer_suggestions(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Suggest pairs the matcher couldn't auto-confirm (score 50–74)."""
    from app.services.transfer_matcher import find_transfer_pairs, SCORE_THRESHOLD_AUTO
    candidates = await find_transfer_pairs(db)
    suggestions = [
        {
            "out_transaction_id": c.a.id,
            "in_transaction_id": c.b.id,
            "out_account_id": c.a.account_id,
            "in_account_id": c.b.account_id,
            "amount": str(c.a.amount),
            "currency": c.a.currency,
            "out_date": c.a.occurred_at,
            "in_date": c.b.occurred_at,
            "out_description": c.a.description,
            "in_description": c.b.description,
            "score": c.score,
            "reasons": c.reasons,
        }
        for c in candidates
        if c.score < SCORE_THRESHOLD_AUTO
    ]
    return ApiSuccess(data=suggestions)


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


# ─── Inbox: pending transactions awaiting user confirmation ───────────


@router.get("/inbox/list", response_model=ApiSuccess[list[TransactionOut]])
async def list_inbox(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    """All pending transactions (auto-categorized or not). Frontend's confirm queue."""
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.is_pending.is_(True), Transaction.deleted_at.is_(None))
        .order_by(Transaction.occurred_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return ApiSuccess(data=[_tx_to_out(t) for t in rows])


@router.post("/inbox/{transaction_id}/confirm", response_model=ApiSuccess[TransactionOut])
async def confirm_inbox_item(
    transaction_id: int,
    body: TransactionUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Confirm a single inbox item with optional category override.

    If `body.category_id` is set:
      - it overrides any auto-suggested category
      - triggers `learn_from_user_assignment` to derive a rule

    Sets `is_pending=False` after confirmation.
    """
    from app.services.categorizer.engine import learn_from_user_assignment

    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.is_pending.is_(True),
               Transaction.deleted_at.is_(None))
    )
    tx = (await db.execute(stmt)).scalar_one_or_none()
    if not tx:
        raise NotFoundError("Pending Transaction", transaction_id)

    old_category_id = tx.category_id
    update_data = body.model_dump(exclude_unset=True)
    new_category_id = update_data.get("category_id", old_category_id)

    # Apply user overrides (description / category / etc.)
    for key, value in update_data.items():
        if key == "amount" and value is not None:
            value = Decimal(value)
        setattr(tx, key, value)

    tx.is_pending = False
    touch_updated_at(tx)
    await db.flush()

    # Learn ONLY when user actually changed (or chose) the category.
    # Confirming an auto-suggested category that wasn't changed → don't strengthen
    # (the rule already matched, no new signal).
    if new_category_id is not None and new_category_id != old_category_id:
        await learn_from_user_assignment(db, tx, new_category_id)

    # Refresh cashflow snapshot — a confirmed tx now contributes
    period = parse_period(tx.occurred_at)
    if period:
        await recompute_period(db, period[0], period[1])

    return ApiSuccess(data=_tx_to_out(tx))
