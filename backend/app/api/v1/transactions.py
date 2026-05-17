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
from app.core.errors import InvalidInputError, NotFoundError
from app.db import get_db
from app.models import Transaction, Account, Category
from app.models import touch_updated_at
from app.schemas import (
    ApiSuccess,
    MarkTransferIn,
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


async def _validate_kind_match(
    db: AsyncSession,
    *,
    tx_type: str,
    category_id: int | None,
) -> None:
    """Sprint 1 FIX-5 (review V1 §P1-4) invariant: when a transaction has
    a category assigned, the category's kind must match the transaction's
    type. Otherwise dashboards / breakdown views silently hide rows.

    Raises ``InvalidInputError`` (HTTP 422) if mismatched. Categories that
    don't exist also raise.
    """
    if category_id is None:
        return
    cat = (await db.execute(
        select(Category).where(Category.id == category_id)
    )).scalar_one_or_none()
    if cat is None:
        raise InvalidInputError(
            f"Category {category_id} does not exist.",
            details={"category_id": category_id},
        )
    if cat.kind != tx_type:
        raise InvalidInputError(
            f"Category kind '{cat.kind}' does not match transaction type "
            f"'{tx_type}'. Pick a category whose kind == type.",
            details={"category_id": category_id, "category_kind": cat.kind, "tx_type": tx_type},
        )


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
    # Sprint 2 FIX-12 (review §P2-6): build the filter clause once and apply it
    # to BOTH the data query and the count query, so the front-end's pagination
    # `total` actually reflects the filtered set.
    def _apply_filters(s):
        if account_id is not None:
            s = s.where(Transaction.account_id == account_id)
        if category_id is not None:
            s = s.where(Transaction.category_id == category_id)
        if type is not None:
            s = s.where(Transaction.type == type)
        if from_date is not None:
            s = s.where(Transaction.occurred_at >= from_date)
        if to_date is not None:
            s = s.where(Transaction.occurred_at < to_date + "T23:59:59Z")
        if min_amount is not None:
            s = s.where(Transaction.amount >= Decimal(min_amount))
        if max_amount is not None:
            s = s.where(Transaction.amount <= Decimal(max_amount))
        if search is not None:
            pattern = f"%{search}%"
            s = s.where(
                or_(
                    Transaction.description.ilike(pattern),
                    Transaction.counterparty.ilike(pattern),
                    Transaction.raw_description.ilike(pattern),
                )
            )
        if tags is not None:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            for tag in tag_list:
                s = s.where(Transaction.tags_json.contains(tag))
        if source is not None:
            s = s.where(Transaction.source == source)
        if is_pending is not None:
            s = s.where(Transaction.is_pending == (1 if is_pending else 0))
        return s

    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.deleted_at.is_(None))
        .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
    )
    stmt = _apply_filters(stmt)

    # Cursor-based pagination — applies only to the page query, not the count.
    if cursor is not None:
        stmt = stmt.where(Transaction.id < cursor)

    stmt = stmt.limit(limit + 1)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    # Count total — same filter clause, no cursor.
    count_stmt = _apply_filters(
        select(func.count(Transaction.id)).where(Transaction.deleted_at.is_(None))
    )

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
    # Sprint 1 FIX-5 (review V1 §P1-4): Category.kind must match Transaction.type.
    await _validate_kind_match(db, tx_type=body.type, category_id=body.category_id)
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
    # Sprint 1 FIX-4: route through unified ingestion (amount normalize +
    # categorize + cashflow recompute). `auto_pair=False` — running the full
    # matcher for one row is wasteful; users can hit /transfers/suggestions.
    from app.services.ingestion import ingest_transactions

    await ingest_transactions(db, [tx], auto_pair=False)
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
    # Sprint 1 FIX-5: validate kind invariant for every row before creating any.
    for t_data in body.transactions:
        await _validate_kind_match(db, tx_type=t_data.type, category_id=t_data.category_id)
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
    # Sprint 1 FIX-4: unified ingestion (amount normalize + categorize +
    # transfer match across the batch + cashflow recompute).
    from app.services.ingestion import ingest_transactions

    await ingest_transactions(db, created, auto_pair=True)
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
    apply_scope: str = Query("all", pattern="^(all|single|never)$"),
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

    # Sprint 4 FIX-19 (review V3 §V3-P0-2): when amount or currency changes,
    # the previously stored base_amount and fx_rate_to_base become stale.
    # Clear both so the ingestion pipeline Step 1.5 can recompute them via
    # resolve_fx_to_base. If the caller explicitly patches fx_rate_to_base /
    # base_amount they take precedence (no clearing for those fields).
    amount_or_currency_changed = "amount" in update_data or "currency" in update_data
    if amount_or_currency_changed:
        if "base_amount" not in update_data:
            update_data["base_amount"] = None
        if "fx_rate_to_base" not in update_data:
            update_data["fx_rate_to_base"] = None

    # Sprint 1 FIX-5: enforce kind invariant on the *resulting* (type, category)
    # pair — one or both fields may be in the patch.
    final_type = update_data.get("type", tx.type)
    final_cat_id = update_data.get("category_id", tx.category_id)
    await _validate_kind_match(db, tx_type=final_type, category_id=final_cat_id)

    # Sprint 3 FIX-15 (review V2 closes V1 P1-5): non-adjustment rows are
    # always stored as ABS(amount). Apply the same invariant the ingestion
    # pipeline enforces, so PATCH can't reintroduce signed amounts.
    if "amount" in update_data and update_data["amount"] is not None:
        if final_type != "adjustment" and update_data["amount"] < 0:
            update_data["amount"] = -update_data["amount"]

    for key, value in update_data.items():
        setattr(tx, key, value)

    touch_updated_at(tx)
    await db.flush()

    # Re-fold FX after the patch lands (idempotent; ingest_transactions Step 1.5
    # fills base_amount/fx_rate_to_base when they're NULL).
    if amount_or_currency_changed:
        from app.services.ingestion import ingest_transactions
        await ingest_transactions(db, [tx], auto_pair=False, skip_categorize=True)

    # Auto-learn: if user changed the category, derive (or strengthen) a rule.
    # `apply_scope` (query param) lets the user opt out per-call:
    #   - "all"    (default): learn rule + cascade to siblings
    #   - "single": skip both (one-off correction)
    #   - "never":  skip both AND disable any existing rule for this keyword
    new_category_id = tx.category_id
    new_period = parse_period(tx.occurred_at)
    affected_periods = [old_period, new_period]
    if (
        new_category_id is not None
        and new_category_id != old_category_id
        and tx.source != "manual"  # only learn from imported tx where description is "real" merchant text
    ):
        from app.services.categorizer.engine import (
            apply_to_similar_pending,
            disable_rules_for_keyword,
            learn_from_user_assignment,
            record_note_to_kb,
        )
        if apply_scope == "all":
            await learn_from_user_assignment(db, tx, new_category_id)
            # If the user attached a free-form note, persist it to the KB
            # so the LLM can use it next time AND mark same-keyword rules
            # as requires_llm so they re-route to L2.
            if tx.user_note:
                await record_note_to_kb(
                    db, tx=tx, new_category_id=new_category_id, note_text=tx.user_note
                )
            cascaded = await apply_to_similar_pending(db, tx, new_category_id)
            if cascaded:
                from sqlalchemy import select as _select
                cascaded_rows = (await db.execute(
                    _select(Transaction.occurred_at).where(
                        Transaction.category_id == new_category_id,
                        Transaction.id != tx.id,
                    )
                )).all()
                for (occ,) in cascaded_rows:
                    affected_periods.append(parse_period(occ))
        elif apply_scope == "never":
            await disable_rules_for_keyword(db, tx)

    await recompute_for_periods(db, affected_periods)
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

    # 2026-05-07: when deleting a paired transfer leg, also detach the
    # counterpart so it doesn't carry a dangling counter_account_id pointing
    # at a deleted row (which would silently exclude it from the unpaired
    # panel). Without this fix, deleting a synthetic mirror left the source
    # in a "looks paired but isn't" zombie state.
    affected_periods: list[tuple[int, int] | None] = [parse_period(tx.occurred_at)]
    src_meta: dict = {}
    if tx.metadata_json:
        try:
            src_meta = json.loads(tx.metadata_json) or {}
        except (json.JSONDecodeError, TypeError):
            src_meta = {}
    if not isinstance(src_meta, dict):
        src_meta = {}
    paired_id = src_meta.get("paired_with_tx_id")
    if paired_id is not None:
        counterpart = (await db.execute(
            select(Transaction).where(
                Transaction.id == paired_id,
                Transaction.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if counterpart is not None:
            ctr_meta: dict = {}
            if counterpart.metadata_json:
                try:
                    ctr_meta = json.loads(counterpart.metadata_json) or {}
                except (json.JSONDecodeError, TypeError):
                    ctr_meta = {}
            if not isinstance(ctr_meta, dict):
                ctr_meta = {}
            counterpart.counter_account_id = None
            ctr_meta.pop("paired_with_tx_id", None)
            counterpart.metadata_json = (
                json.dumps(ctr_meta, sort_keys=True, ensure_ascii=False)
                if ctr_meta else None
            )
            affected_periods.append(parse_period(counterpart.occurred_at))

    # Also clear our own paired_with_tx_id so a future refresh-matching can't
    # re-pair us with a tombstoned counterpart (Step -1 only inspects active
    # rows, so the stale pointer would otherwise survive forever).
    if "paired_with_tx_id" in src_meta:
        src_meta.pop("paired_with_tx_id", None)
        tx.metadata_json = (
            json.dumps(src_meta, sort_keys=True, ensure_ascii=False)
            if src_meta else None
        )

    tx.deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.flush()
    await recompute_for_periods(db, affected_periods)
    return ApiSuccess(data={"id": transaction_id, "deleted": True})


# ─── Transfer matching ────────────────────────────────────────────────


@router.post("/{transaction_id}/mark-transfer", response_model=ApiSuccess[TransactionOut])
async def mark_as_transfer(
    transaction_id: int,
    body: MarkTransferIn,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Mark a single tx as a transfer; optionally pair with a counter-leg.

    Without `counter_transaction_id`: requires `transfer_direction`, flips type →
    'transfer' and tags direction in metadata_json so the balance view applies the
    correct sign.
    With `counter_transaction_id`: routes through pair_transactions() which tags
    both legs correctly. `transfer_direction` describes the URL tx (the out/in
    leg); if omitted, falls back to tx.type heuristic.
    """
    from app.services.transfer_matcher.engine import _merge_meta, pair_transactions

    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    tx = (await db.execute(stmt)).scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    # If user picked a category in the dialog, it must be a kind='transfer'
    # one — anything else would silently misbucket dashboards.
    if body.category_id is not None:
        cat = (await db.execute(
            select(Category).where(Category.id == body.category_id)
        )).scalar_one_or_none()
        if cat is None:
            raise InvalidInputError(
                f"Category {body.category_id} does not exist.",
                details={"category_id": body.category_id},
            )
        if cat.kind != "transfer":
            raise InvalidInputError(
                f"Category kind '{cat.kind}' is not allowed for a transfer. "
                "Pick a category whose kind == 'transfer'.",
                details={"category_id": body.category_id, "category_kind": cat.kind},
            )

    old_period = parse_period(tx.occurred_at)

    if body.counter_transaction_id is not None:
        ctr = (await db.execute(
            select(Transaction).where(
                Transaction.id == body.counter_transaction_id,
                Transaction.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not ctr:
            raise NotFoundError("Counter Transaction", body.counter_transaction_id)

        # V4-P1-2: pair invariants. Without these, an API caller (or stale
        # UI) could pair a 100 EUR expense with a 500 CNY income, both
        # would flip to type='transfer' and drop out of income/expense
        # totals — silently distorting cashflow.
        if ctr.account_id == tx.account_id:
            raise InvalidInputError(
                "Counter transaction is in the same account; not a cross-account transfer.",
                details={"account_id": tx.account_id, "counter_tx_id": ctr.id},
            )
        if ctr.currency != tx.currency:
            raise InvalidInputError(
                "Currency mismatch between the two legs; cross-currency "
                "transfers are not supported in manual binding.",
                details={
                    "tx_currency": tx.currency, "counter_currency": ctr.currency,
                },
            )
        try:
            tx_amt = Decimal(str(tx.amount))
            ctr_amt = Decimal(str(ctr.amount))
        except Exception:
            raise InvalidInputError("Invalid amount on one of the legs.")
        # Tolerance defaults to 0.01 (cent precision). Manual flows
        # (paying-for-friends, uneven split) pass a larger value to bind
        # legs that legitimately differ by a few units. We still cap at a
        # reasonable upper bound to avoid wildly mismatched pairs.
        try:
            tol = Decimal(body.amount_tolerance) if body.amount_tolerance else Decimal("0.01")
        except Exception:
            raise InvalidInputError(
                f"Invalid amount_tolerance: {body.amount_tolerance!r}"
            )
        if tol < Decimal("0"):
            tol = Decimal("0")
        if tol > Decimal("10000"):
            raise InvalidInputError(
                "amount_tolerance is unreasonably large (> 10000)."
            )
        diff = abs(tx_amt - ctr_amt)
        # tol=0 demands exact equality; otherwise allow up to and including tol
        if (tol == 0 and diff != 0) or (tol > 0 and diff > tol):
            raise InvalidInputError(
                "Amount mismatch between the two legs.",
                details={
                    "tx_amount": str(tx_amt),
                    "counter_amount": str(ctr_amt),
                    "diff": str(diff),
                    "tolerance": str(tol),
                },
            )
        # Already-bound check on the counter side. If `ctr` is paired to a
        # SYNTHETIC mirror leg (created by an earlier "bind to account"
        # action), retire that synthetic and proceed with the real-pair.
        # Otherwise refuse — re-binding would orphan the real partner.
        if ctr.counter_account_id is not None and ctr.counter_account_id != tx.account_id:
            ctr_meta_existing: dict = {}
            if ctr.metadata_json:
                try:
                    ctr_meta_existing = json.loads(ctr.metadata_json) or {}
                except (json.JSONDecodeError, TypeError):
                    ctr_meta_existing = {}
            if not isinstance(ctr_meta_existing, dict):
                ctr_meta_existing = {}
            existing_partner_id = ctr_meta_existing.get("paired_with_tx_id")
            existing_partner: Transaction | None = None
            if existing_partner_id is not None:
                existing_partner = (await db.execute(
                    select(Transaction).where(Transaction.id == existing_partner_id)
                )).scalar_one_or_none()
            partner_is_synthetic = False
            if existing_partner is not None and existing_partner.metadata_json:
                try:
                    pm = json.loads(existing_partner.metadata_json) or {}
                    if isinstance(pm, dict) and pm.get("synthetic_counterleg") is True:
                        partner_is_synthetic = True
                except (json.JSONDecodeError, TypeError):
                    pass

            if partner_is_synthetic and existing_partner is not None:
                # Retire the synthetic mirror — soft-delete + clear ctr's
                # back-pointer + counter_account_id so pair_transactions
                # below can write a clean new pair.
                existing_partner.deleted_at = _utcnow_str()
                touch_updated_at(existing_partner)
                ctr.counter_account_id = None
                ctr_meta_existing.pop("paired_with_tx_id", None)
                ctr_meta_existing.pop("synthetic_counterleg", None)
                ctr.metadata_json = (
                    json.dumps(ctr_meta_existing, ensure_ascii=False, sort_keys=True)
                    if ctr_meta_existing else None
                )
                touch_updated_at(ctr)
                await db.flush()
            else:
                raise InvalidInputError(
                    "Counter transaction is already bound to a different account.",
                    details={"counter_existing_account_id": ctr.counter_account_id},
                )

        # Determine which leg is out and which is in.
        if body.transfer_direction == "out":
            out_tx, in_tx = tx, ctr
        elif body.transfer_direction == "in":
            out_tx, in_tx = ctr, tx
        else:
            # Fallback: use tx.type heuristic
            if tx.type == "expense":
                out_tx, in_tx = tx, ctr
            elif tx.type == "income":
                out_tx, in_tx = ctr, tx
            elif ctr.type == "expense":
                out_tx, in_tx = ctr, tx
            elif ctr.type == "income":
                out_tx, in_tx = tx, ctr
            else:
                raise InvalidInputError(
                    "Cannot determine transfer direction automatically. "
                    "Please supply transfer_direction."
                )

        await pair_transactions(db, out_tx, in_tx)
        # User-picked category overrides whatever the matcher resolved
        # (e.g. user knows it's 投资划转 even though heuristics said 跨行划转).
        if body.category_id is not None:
            out_tx.category_id = body.category_id
            in_tx.category_id = body.category_id
        out_tx.is_pending = False
        in_tx.is_pending = False
        ctr_period = parse_period(ctr.occurred_at)
        await db.flush()
        await recompute_for_periods(db, [old_period, ctr_period])
    elif body.counter_account_id is not None:
        if not body.transfer_direction:
            raise InvalidInputError(
                "transfer_direction is required when only counter_account_id is given."
            )
        if body.counter_account_id == tx.account_id:
            raise InvalidInputError(
                "counter_account_id must differ from the source account."
            )
        # 2026-05-07: guard against double-bind. If the source tx already
        # has a counter_account_id (e.g. user bound the OTHER leg first and
        # then clicks bind on this leg from a stale UI), refuse — otherwise
        # we'd overwrite the existing pair pointer and orphan the real
        # counterpart on the other side.
        # Force a re-read so this guard reflects the freshest state if a
        # concurrent refresh-matching just cleared the pointer.
        await db.refresh(tx, attribute_names=["counter_account_id"])
        if tx.counter_account_id is not None:
            raise InvalidInputError(
                "Transaction is already bound to a counter account "
                f"(account_id={tx.counter_account_id}). Unbind first to re-pair.",
                details={
                    "current_counter_account_id": tx.counter_account_id,
                    "requested_counter_account_id": body.counter_account_id,
                },
            )
        ctr_account = (await db.execute(
            select(Account).where(
                Account.id == body.counter_account_id,
                Account.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if ctr_account is None:
            raise NotFoundError("Counter Account", body.counter_account_id)

        # V4-P1-2: synthetic mirror copies the source's amount + currency
        # verbatim into the counter account. If the counter account has a
        # different currency, the mirror would write e.g. 100 EUR into a
        # CNY account — the balance view would then add raw "100" to the
        # CNY balance, polluting its unit. Refuse cross-currency
        # synthesise; the user must instead create both legs explicitly
        # with FX, or pair an existing real counterpart.
        if ctr_account.currency != tx.currency:
            raise InvalidInputError(
                "Cross-currency synthetic mirror is not supported. "
                f"Source is {tx.currency}, counter account is "
                f"{ctr_account.currency}. Create the counter leg manually "
                "with the correct currency + FX, or pick a same-currency "
                "counter account.",
                details={
                    "src_currency": tx.currency,
                    "counter_currency": ctr_account.currency,
                },
            )

        # 2026-05-07: before synthesising a mirror leg, look for an existing
        # real tx in the destination that the matcher missed. Without this,
        # binding both sides of a real transfer (because both showed up in
        # the unpaired panel) would produce duplicate +/-500 rows on each
        # side. Find a same-amount/currency unpaired tx in the counter
        # account and pair to that.
        from app.services.transfer_matcher.engine import find_existing_counter_leg
        existing_real = await find_existing_counter_leg(
            db, src_tx=tx, counter_account_id=body.counter_account_id,
        )
        if existing_real is not None:
            if body.transfer_direction == "out":
                out_tx, in_tx = tx, existing_real
            else:
                out_tx, in_tx = existing_real, tx
            await pair_transactions(db, out_tx, in_tx)
            if body.category_id is not None:
                out_tx.category_id = body.category_id
                in_tx.category_id = body.category_id
            out_tx.is_pending = False
            in_tx.is_pending = False
            ctr_period = parse_period(existing_real.occurred_at)
            await db.flush()
            await recompute_for_periods(db, [old_period, ctr_period])
            return ApiSuccess(data=_tx_to_out(tx))

        # No real counter-leg found → synthesise one. This keeps the
        # cross-account move visible on both balances even when the user
        # never imports / will never import the destination's PDF (e.g.
        # external accounts, banks that don't issue downloadable statements).
        opposite_dir = "in" if body.transfer_direction == "out" else "out"
        # Idempotency: if the user re-submits the same mark-transfer call we
        # don't want to double-create the mirror.
        existing_mirror = (await db.execute(
            select(Transaction).where(
                Transaction.account_id == body.counter_account_id,
                Transaction.external_id == f"counterleg_of_{tx.id}",
                Transaction.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if existing_mirror is None:
            mirror = Transaction(
                account_id=body.counter_account_id,
                counter_account_id=tx.account_id,
                category_id=body.category_id,
                occurred_at=tx.occurred_at,
                amount=tx.amount,
                currency=tx.currency,
                type="transfer",
                description=tx.description,
                raw_description=tx.raw_description,
                source="manual",
                external_id=f"counterleg_of_{tx.id}",
                is_pending=False,
                metadata_json=_merge_meta(None, {
                    "transfer_direction": opposite_dir,
                    "synthetic_counterleg": True,
                    "paired_with_tx_id": tx.id,
                }),
            )
            db.add(mirror)
            await db.flush()
        else:
            mirror = existing_mirror

        # Source side: type, direction, counter-account, paired-id, category
        tx.type = "transfer"
        tx.counter_account_id = body.counter_account_id
        if body.category_id is not None:
            tx.category_id = body.category_id
        tx.is_pending = False
        tx.metadata_json = _merge_meta(tx.metadata_json, {
            "transfer_direction": body.transfer_direction,
            "paired_with_tx_id": mirror.id,
        })

        ctr_period = parse_period(mirror.occurred_at)
        await db.flush()
        await recompute_for_periods(db, [old_period, ctr_period])
    else:
        # Single-leg & no counter account: external destination (gift, vendor
        # not in this system, etc.). Source balance drops; we accept that
        # because the counterparty isn't tracked here.
        if not body.transfer_direction:
            raise InvalidInputError(
                "transfer_direction is required when no counter_transaction_id is given."
            )
        tx.type = "transfer"
        if body.category_id is not None:
            tx.category_id = body.category_id
        tx.is_pending = False
        tx.metadata_json = _merge_meta(tx.metadata_json, {"transfer_direction": body.transfer_direction})
        await db.flush()
        await recompute_for_periods(db, [old_period])

    return ApiSuccess(data=_tx_to_out(tx))


@router.post("/{transaction_id}/unbind-counter", response_model=ApiSuccess[dict])
async def unbind_counter(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Detach a transfer's counter-account binding so it returns to 未配对.

    Symmetric: if the bound counterpart is a synthetic mirror (created by an
    earlier `mark-transfer` call), it gets soft-deleted; if it's a real
    transaction, its own counter pointer is cleared so it shows up in 未配对
    too. Both sides become re-bindable.

    The transactions remain `type='transfer'` and keep their category — only
    the pairing pointers (counter_account_id + metadata.paired_with_tx_id)
    are cleared. The user can re-bind via the 转账建议 panel.
    """
    from datetime import datetime, timezone
    from app.services.transfer_matcher.engine import _merge_meta

    tx = (await db.execute(
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )).scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    # Collect periods to recompute
    periods: list[tuple[int, int] | None] = [parse_period(tx.occurred_at)]
    counterpart: Transaction | None = None
    deleted_synthetic = False

    # Find the counterpart, if any
    src_meta: dict = {}
    if tx.metadata_json:
        try:
            src_meta = json.loads(tx.metadata_json) or {}
        except (json.JSONDecodeError, TypeError):
            src_meta = {}
    if not isinstance(src_meta, dict):
        src_meta = {}
    paired_id = src_meta.get("paired_with_tx_id")
    if paired_id is not None:
        counterpart = (await db.execute(
            select(Transaction).where(
                Transaction.id == paired_id,
                Transaction.deleted_at.is_(None),
            )
        )).scalar_one_or_none()

    if counterpart is not None:
        periods.append(parse_period(counterpart.occurred_at))
        ctr_meta: dict = {}
        if counterpart.metadata_json:
            try:
                ctr_meta = json.loads(counterpart.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                ctr_meta = {}
        if not isinstance(ctr_meta, dict):
            ctr_meta = {}
        if ctr_meta.get("synthetic_counterleg") is True:
            # Soft-delete — it has no real-world counterpart in any statement
            counterpart.deleted_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            deleted_synthetic = True
        else:
            # Symmetric unbind: detach the real counterpart so it also returns
            # to 未配对 and can be re-bound to a different (or no) account.
            counterpart.counter_account_id = None
            ctr_meta.pop("paired_with_tx_id", None)
            counterpart.metadata_json = json.dumps(ctr_meta, sort_keys=True, ensure_ascii=False) if ctr_meta else None

    # Detach this side
    tx.counter_account_id = None
    src_meta.pop("paired_with_tx_id", None)
    tx.metadata_json = json.dumps(src_meta, sort_keys=True, ensure_ascii=False) if src_meta else None

    await db.flush()
    await recompute_for_periods(db, periods)

    return ApiSuccess(data={
        "transaction_id": transaction_id,
        "counterpart_id": counterpart.id if counterpart else None,
        "deleted_synthetic": deleted_synthetic,
    })


@router.get("/recently-promoted-to-transfer", response_model=ApiSuccess[list[dict]])
async def list_recently_promoted(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
):
    """List rows whose `type` was auto-flipped from expense/income to
    transfer by a recent `refresh-matching` run.

    These carry `metadata.type_promoted_by='refresh_matching'` plus the
    timestamp and original type. Use this to spot false positives —
    e.g. a real expense that happened to contain a sub-account name like
    "saving" but wasn't actually a transfer.

    The frontend can later wrap this in a UI; for now it's CLI-callable.
    """
    from sqlalchemy import text as _text
    promoted_filter = _text(
        "metadata_json IS NOT NULL "
        "AND json_valid(metadata_json) "
        "AND json_extract(metadata_json, '$.type_promoted_by') = 'refresh_matching'"
    )
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(
            Transaction.deleted_at.is_(None),
            promoted_filter,
        )
        .order_by(Transaction.occurred_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict] = []
    for r in rows:
        meta: dict = {}
        if r.metadata_json:
            try:
                meta = json.loads(r.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        out.append({
            "transaction_id": r.id,
            "account_id": r.account_id,
            "account_name": r.account.name if r.account else None,
            "category_id": r.category_id,
            "category_name": r.category.name if r.category else None,
            "occurred_at": r.occurred_at,
            "amount": str(r.amount),
            "currency": r.currency,
            "description": r.description,
            "current_type": r.type,
            "original_type": meta.get("type_promoted_from"),
            "promoted_at": meta.get("type_promoted_at"),
        })
    return ApiSuccess(data=out)


@router.post("/{transaction_id}/revert-type-promotion", response_model=ApiSuccess[TransactionOut])
async def revert_type_promotion(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Undo a `refresh-matching` type promotion. Restores the original
    type stored in `metadata.type_promoted_from`, clears the audit stamp,
    and unbinds any counter pair created downstream (since the row is no
    longer a transfer)."""
    tx = (await db.execute(
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )).scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)

    meta: dict = {}
    if tx.metadata_json:
        try:
            meta = json.loads(tx.metadata_json) or {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if not isinstance(meta, dict) or meta.get("type_promoted_by") != "refresh_matching":
        raise InvalidInputError(
            "This transaction was not auto-promoted by refresh-matching; "
            "nothing to revert.",
        )
    original = meta.get("type_promoted_from")
    if original not in ("expense", "income"):
        raise InvalidInputError(
            f"Cannot revert: stored original type {original!r} is not expense/income.",
        )

    # If a counter binding exists from the promotion, detach it (synthetic
    # mirror was created downstream of the type flip — without unbinding
    # it would leak into the unpaired panel as an orphan).
    paired_id = meta.get("paired_with_tx_id")
    if paired_id is not None:
        counterpart = (await db.execute(
            select(Transaction).where(
                Transaction.id == paired_id, Transaction.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if counterpart is not None:
            ctr_meta: dict = {}
            if counterpart.metadata_json:
                try:
                    ctr_meta = json.loads(counterpart.metadata_json) or {}
                except (json.JSONDecodeError, TypeError):
                    ctr_meta = {}
            if ctr_meta.get("synthetic_counterleg") is True:
                from datetime import datetime, timezone
                counterpart.deleted_at = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            else:
                counterpart.counter_account_id = None
                ctr_meta.pop("paired_with_tx_id", None)
                counterpart.metadata_json = (
                    json.dumps(ctr_meta, sort_keys=True, ensure_ascii=False)
                    if ctr_meta else None
                )

    tx.type = original
    tx.counter_account_id = None
    # Drop transfer-specific metadata + audit stamps. Keep anything else
    # (e.g. fx_missing) intact.
    for key in (
        "type_promoted_by", "type_promoted_at", "type_promoted_from",
        "paired_with_tx_id", "transfer_direction", "subaccount",
        "matched", "source", "synthetic_counterleg",
    ):
        meta.pop(key, None)
    tx.metadata_json = json.dumps(meta, sort_keys=True, ensure_ascii=False) if meta else None
    # Reset category since the new (old) type's kind invariant might not
    # match the currently-set transfer category.
    tx.category_id = None
    tx.is_pending = True  # back into inbox so user can re-categorise

    period = parse_period(tx.occurred_at)
    await db.flush()
    await recompute_for_periods(db, [period])
    return ApiSuccess(data=_tx_to_out(tx))


@router.get("/transfers/unpaired", response_model=ApiSuccess[list[dict]])
async def list_unpaired_transfers(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
):
    """All `type='transfer'` rows that don't have a confirmed counter-leg.

    Filters (pushed down to SQL via json_extract so we don't load the whole
    transfers table into memory just to drop most of it):
      - type = 'transfer'
      - deleted_at IS NULL
      - counter_account_id IS NULL                ← no internal pair
      - metadata.subaccount != 1                  ← in-bank sub-account
        moves are intentionally single-leg
      - metadata.paired_with_tx_id IS NULL        ← matcher didn't pair it

    Used by the front-end "转账建议" panel to surface every cross-bank
    transfer the user still needs to bind a counter account to. Global —
    scans all history, capped at `limit`.
    """
    from sqlalchemy import text as _text

    not_subaccount = _text(
        "(metadata_json IS NULL OR NOT json_valid(metadata_json) "
        "OR COALESCE(json_extract(metadata_json, '$.subaccount'), 0) != 1)"
    )
    not_paired_meta = _text(
        "(metadata_json IS NULL OR NOT json_valid(metadata_json) "
        "OR json_extract(metadata_json, '$.paired_with_tx_id') IS NULL)"
    )
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(
            Transaction.type == "transfer",
            Transaction.deleted_at.is_(None),
            Transaction.counter_account_id.is_(None),
            not_subaccount,
            not_paired_meta,
        )
        .order_by(Transaction.occurred_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict] = []
    for r in rows:
        meta: dict = {}
        if r.metadata_json:
            try:
                meta = json.loads(r.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        out.append({
            "transaction_id": r.id,
            "account_id": r.account_id,
            "account_name": r.account.name if r.account else None,
            "category_id": r.category_id,
            "category_name": r.category.name if r.category else None,
            "occurred_at": r.occurred_at,
            "amount": str(r.amount),
            "currency": r.currency,
            "description": r.description,
            "raw_description": r.raw_description,
            "transfer_direction": meta.get("transfer_direction"),
        })
    return ApiSuccess(data=out)


@router.get(
    "/transfers/{transaction_id}/counter-leg-candidates",
    response_model=ApiSuccess[list[dict]],
)
async def list_counter_leg_candidates_for_tx(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    window_days: int = Query(10, ge=1, le=30),
    amount_tolerance: str = Query(
        "0.01",
        description=(
            "Allowed amount diff between source and candidate (same currency). "
            "Default 0.01 enforces cent precision; pass a larger number to "
            "include candidates that differ by more (e.g. friend's reimbursement)."
        ),
    ),
):
    """List unpaired tx in OTHER accounts that the user can manually bind
    as the counter-leg of `transaction_id`.

    Returns same-currency, ±`amount_tolerance`-amount, ±`window_days`,
    NOT-paired, NOT-synthetic, NOT-subaccount tx. Powers the manual-pair
    dialog in the "未配对转账" panel for cases where automatic matching
    missed the pair (e.g. AMEX took 5+ days to settle, or the friend
    rounded the reimbursement amount).
    """
    from app.services.transfer_matcher import list_counter_leg_candidates

    src = (await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if src is None:
        raise NotFoundError("Transaction", transaction_id)

    try:
        tol = Decimal(amount_tolerance)
    except Exception:
        raise InvalidInputError(
            f"Invalid amount_tolerance: {amount_tolerance!r}"
        )
    if tol < Decimal("0"):
        tol = Decimal("0")
    if tol > Decimal("10000"):
        raise InvalidInputError(
            "amount_tolerance is unreasonably large (> 10000)."
        )

    candidates = await list_counter_leg_candidates(
        db, src_tx=src, window_days=window_days, amount_tolerance=tol,
    )
    if not candidates:
        return ApiSuccess(data=[])

    # `candidates` is now a list of (Transaction, status) tuples.
    cand_tuples = candidates
    # Hydrate account names in one query
    acct_ids = {c.account_id for (c, _s) in cand_tuples}
    acct_rows = (await db.execute(
        select(Account.id, Account.name).where(Account.id.in_(acct_ids))
    )).all()
    name_by_id = {aid: aname for aid, aname in acct_rows}

    # Compute days_diff for sorting + UI display
    from datetime import datetime
    try:
        src_d = datetime.strptime(src.occurred_at[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        src_d = None

    src_amt = Decimal(str(src.amount))
    out: list[dict] = []
    for (c, status) in cand_tuples:
        days_diff = None
        if src_d is not None:
            try:
                c_d = datetime.strptime(c.occurred_at[:10], "%Y-%m-%d")
                days_diff = abs((c_d - src_d).days)
            except (ValueError, TypeError):
                days_diff = None
        c_amt = Decimal(str(c.amount))
        amount_diff = c_amt - src_amt  # signed: positive = candidate > source
        out.append({
            "transaction_id": c.id,
            "account_id": c.account_id,
            "account_name": name_by_id.get(c.account_id),
            "occurred_at": c.occurred_at,
            "amount": str(c.amount),
            "amount_diff": str(amount_diff),  # signed; UI formats sign
            "currency": c.currency,
            "type": c.type,
            "description": c.description,
            "raw_description": c.raw_description,
            "days_diff": days_diff,
            # status='free' (clean) or 'synthetic_bound' (re-pairs after
            # retiring the existing synthetic mirror).
            "status": status,
        })
    # Sort: free candidates first, then smallest |amount_diff|, then closest
    # by date, then by occurred_at.
    out.sort(key=lambda r: (
        0 if r["status"] == "free" else 1,
        abs(Decimal(r["amount_diff"])),
        r["days_diff"] if r["days_diff"] is not None else 999,
        r["occurred_at"],
    ))
    return ApiSuccess(data=out)


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


@router.get("/{transaction_id}/similar-count", response_model=ApiSuccess[dict])
async def preview_similar_count(
    transaction_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    category_id: int | None = Query(None),
):
    """Preview: how many sibling tx would `apply_scope=all` cascade to?

    Frontend calls this before showing the scope dialog so the
    "应用到所有同名（共 N 条）" button can show an accurate count.
    Also returns the keyword that would be learned (or null if
    no stable keyword can be derived from this tx).
    """
    from app.services.categorizer.engine import (
        count_similar_pending,
        derive_keyword_for_tx,
    )

    stmt = (
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
    )
    tx = (await db.execute(stmt)).scalar_one_or_none()
    if not tx:
        raise NotFoundError("Transaction", transaction_id)
    target_cat = category_id if category_id is not None else (tx.category_id or 0)
    count = await count_similar_pending(db, tx, target_cat)
    derived = derive_keyword_for_tx(tx)
    keyword = derived[1] if derived else None
    return ApiSuccess(data={"count": count, "keyword": keyword})


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
    apply_scope: str = Query("all", pattern="^(all|single|never)$"),
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

    # Sprint 4 FIX-19 (review V3 §V3-P0-2): clear stale FX fields when amount
    # or currency changes, so the re-fold pass below recomputes them cleanly.
    inbox_amount_or_currency_changed = "amount" in update_data or "currency" in update_data
    if inbox_amount_or_currency_changed:
        if "base_amount" not in update_data:
            update_data["base_amount"] = None
        if "fx_rate_to_base" not in update_data:
            update_data["fx_rate_to_base"] = None

    # Sprint 1 FIX-5: enforce kind invariant on the resulting (type, category).
    final_type = update_data.get("type", tx.type)
    await _validate_kind_match(db, tx_type=final_type, category_id=new_category_id)

    # Apply user overrides (description / category / etc.)
    for key, value in update_data.items():
        if key == "amount" and value is not None:
            value = Decimal(value)
            # Sprint 3 FIX-15 (review V2 closes V1 P1-5): non-adjustment
            # rows must store ABS(amount). Inbox confirm is a write path
            # that previously bypassed the ingestion-level invariant.
            if final_type != "adjustment" and value < 0:
                value = -value
        setattr(tx, key, value)

    tx.is_pending = False
    touch_updated_at(tx)
    await db.flush()

    # Re-fold FX after the confirm lands (idempotent; ingest_transactions Step 1.5
    # fills base_amount/fx_rate_to_base when they're NULL).
    if inbox_amount_or_currency_changed:
        from app.services.ingestion import ingest_transactions
        await ingest_transactions(db, [tx], auto_pair=False, skip_categorize=True)

    # Learn ONLY when user actually changed (or chose) the category.
    # `apply_scope` mirrors update_transaction:
    #   - "all"    (default): learn rule + cascade to identical-description siblings
    #   - "single": skip both
    #   - "never":  skip both AND disable any existing rule for this keyword
    affected_periods = [parse_period(tx.occurred_at)]
    if new_category_id is not None and new_category_id != old_category_id:
        if apply_scope == "all":
            await learn_from_user_assignment(db, tx, new_category_id)
            # If the user attached a free-form note at inbox time, persist
            # it to the KB so the LLM can use it next time + mark same
            # keyword L1 rules as requires_llm.
            if tx.user_note:
                from app.services.categorizer.engine import record_note_to_kb
                await record_note_to_kb(
                    db, tx=tx, new_category_id=new_category_id, note_text=tx.user_note
                )
            from app.services.categorizer.engine import apply_to_similar_pending
            cascaded = await apply_to_similar_pending(db, tx, new_category_id)
            if cascaded:
                from sqlalchemy import select as _select
                cascaded_rows = (await db.execute(
                    _select(Transaction.occurred_at).where(
                        Transaction.category_id == new_category_id,
                        Transaction.id != tx.id,
                        Transaction.is_pending.is_(False),
                    )
                )).all()
                for (occ,) in cascaded_rows:
                    affected_periods.append(parse_period(occ))
        elif apply_scope == "never":
            from app.services.categorizer.engine import disable_rules_for_keyword
            await disable_rules_for_keyword(db, tx)

    # Refresh cash-flow snapshots for all affected months (de-duplicated)
    await recompute_for_periods(db, affected_periods)

    return ApiSuccess(data=_tx_to_out(tx))
