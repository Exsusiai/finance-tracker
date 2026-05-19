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
from app.models import Account, Transaction
from app.models import touch_updated_at
from app.services.cashflow import parse_period, recompute_period
from app.services.wallet_sync.holdings_value import (
    compute_holdings_value_per_account,
)
from app.schemas import (
    AccountCreate,
    AccountOut,
    AccountUpdate,
    ApiSuccess,
    BalanceAdjustmentIn,
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
        iban=a.iban,
        currency=a.currency,
        initial_balance=_normalize_amount(a.initial_balance),
        is_active=a.is_active,
        include_in_total=a.include_in_total,
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
        iban=body.iban,
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
    """Return current balance for all active accounts.

    Fiat / cash / brokerage accounts read from ``v_account_balance``
    (initial_balance + SUM(transactions)). Crypto wallet / exchange
    accounts have no transactions of their own — their worth lives on
    ``asset_holdings`` × the latest CoinGecko price, so we add that
    value on top (initial_balance is 0 there by design).
    """
    stmt = text("""
        SELECT v.account_id, v.account_name, v.currency, v.balance, a.type
        FROM v_account_balance v
        JOIN accounts a ON a.id = v.account_id
    """)
    rows = (await db.execute(stmt)).all()

    crypto_account_ids = [r[0] for r in rows if r[4] in ("crypto_wallet", "exchange")]
    crypto_value = await compute_holdings_value_per_account(db, crypto_account_ids)

    balances = [
        BalanceOut(
            account_id=r[0],
            account_name=r[1],
            currency=r[2],
            balance=_normalize_amount(Decimal(str(r[3] or 0)) + crypto_value.get(r[0], Decimal("0"))),
        )
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
        SELECT v.account_id, v.account_name, v.currency, v.balance, a.type
        FROM v_account_balance v
        JOIN accounts a ON a.id = v.account_id
        WHERE v.account_id = :aid
    """)
    row = (await db.execute(stmt, {"aid": account_id})).first()
    if not row:
        raise NotFoundError("Account", account_id)
    balance = Decimal(str(row[3] or 0))
    if row[4] in ("crypto_wallet", "exchange"):
        crypto = await compute_holdings_value_per_account(db, [account_id])
        balance += crypto.get(account_id, Decimal("0"))
    return ApiSuccess(data=BalanceOut(
        account_id=row[0], account_name=row[1], currency=row[2],
        balance=_normalize_amount(balance),
    ))


def _extract_subaccount_names(metadata_json: str | None) -> list[str]:
    """Pull out the user-maintained subaccount-name list from
    ``metadata_json``. Returns a lower-cased, deduped list."""
    if not metadata_json:
        return []
    try:
        meta = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return []
    raw = meta.get("subaccount_names") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for n in raw:
        s = str(n).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


async def _reclassify_pending_for_subaccounts(
    db: AsyncSession,
    account_id: int,
    new_subaccount_names: list[str],
) -> int:
    """Re-scan this account's inbox (is_pending pdf_import expense/income
    rows) against the **updated** subaccount-name list. Rows whose
    description / raw_description / counterparty contains any of the names
    get flipped to ``type=transfer`` with ``metadata.subaccount=true`` and
    auto-confirmed (is_pending=False) — same outcome as if the parser had
    seen the new list at PDF-upload time.

    Triggered by PATCH /accounts/{id} when subaccount_names changes, so the
    user doesn't have to re-upload the PDF or hand-classify the rows.
    """
    if not new_subaccount_names:
        return 0

    stmt = select(Transaction).where(
        Transaction.account_id == account_id,
        Transaction.deleted_at.is_(None),
        Transaction.is_pending.is_(True),
        Transaction.source == "pdf_import",
        Transaction.type.in_(("expense", "income")),
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return 0

    from app.services.cashflow import parse_period, recompute_for_periods
    from app.services.transfer_matcher.engine import _merge_meta, _resolve_transfer_category

    # Resolve once — the lookup is constant across the loop.
    subaccount_cat_id = await _resolve_transfer_category(db, kind="subaccount")

    affected_periods: set[tuple[int, int]] = set()
    matched = 0
    for tx in rows:
        haystack = " ".join(filter(None, [
            (tx.description or "").lower(),
            (tx.raw_description or "").lower(),
            (tx.counterparty or "").lower(),
        ]))
        if not haystack:
            continue
        hit = next((n for n in new_subaccount_names if n in haystack), None)
        if not hit:
            continue
        tx.type = "transfer"
        tx.is_pending = False
        # Without a category these rows would re-enter the inbox via the
        # legacy-transfer migration in main.py lifespan.
        if subaccount_cat_id is not None:
            tx.category_id = subaccount_cat_id
        tx.metadata_json = _merge_meta(
            tx.metadata_json,
            {"subaccount": True, "matched": hit, "source": "user_list"},
        )
        period = parse_period(tx.occurred_at)
        if period:
            affected_periods.add(period)
        matched += 1

    if matched:
        await db.flush()
        await recompute_for_periods(db, affected_periods)
    return matched


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

    # Snapshot the OLD subaccount list before applying the patch so we can
    # detect whether the user added new names that should retroactively
    # re-classify already-imported pending transactions.
    old_subaccount_names = set(_extract_subaccount_names(account.metadata_json))

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if key == "initial_balance":
            value = Decimal(value)
        setattr(account, key, value)

    touch_updated_at(account)
    await db.flush()

    # 2026-05-06 fix: when the user adds Investing / Dream List / Saving etc.
    # to subaccount_names AFTER the PDF was already imported, retroactively
    # classify the inbox rows that match the new names — otherwise the user
    # has to hand-classify them all even though the system now knows.
    new_subaccount_names = set(_extract_subaccount_names(account.metadata_json))
    added_names = list(new_subaccount_names - old_subaccount_names)
    reclassified = 0
    if added_names:
        reclassified = await _reclassify_pending_for_subaccounts(
            db, account_id, added_names,
        )

    out = _account_to_out(account)
    meta = {"subaccount_reclassified": reclassified} if added_names else None
    return ApiSuccess(data=out, meta=meta)


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


@router.post(
    "/{account_id}/adjust-balance",
    response_model=ApiSuccess[BalanceOut],
    status_code=status.HTTP_201_CREATED,
)
async def adjust_balance(
    account_id: int,
    body: BalanceAdjustmentIn,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Create an `adjustment` transaction equal to (target_balance - current_balance)."""
    from datetime import datetime, timezone

    acct_stmt = select(Account).where(
        Account.id == account_id, Account.deleted_at.is_(None)
    )
    acct_result = await db.execute(acct_stmt)
    account = acct_result.scalar_one_or_none()
    if not account:
        raise NotFoundError("Account", account_id)

    bal_stmt = text(
        "SELECT balance FROM v_account_balance WHERE account_id = :aid"
    )
    bal_row = (await db.execute(bal_stmt, {"aid": account_id})).first()
    current_balance = Decimal(str(bal_row[0])) if bal_row and bal_row[0] is not None else Decimal("0")

    target = Decimal(body.target_balance)
    delta = target - current_balance

    occurred_at = body.occurred_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    description = body.note or "手动调整余额"

    if delta != 0:
        tx = Transaction(
            account_id=account_id,
            occurred_at=occurred_at,
            amount=delta,
            currency=account.currency,
            type="adjustment",
            description=description,
            source="manual",
            is_pending=False,
        )
        db.add(tx)
        await db.flush()
        # Auto-refresh cashflow snapshot
        period = parse_period(occurred_at)
        if period:
            await recompute_period(db, period[0], period[1])

    new_row = (await db.execute(text(
        "SELECT account_id, account_name, currency, balance FROM v_account_balance WHERE account_id = :aid"
    ), {"aid": account_id})).first()

    return ApiSuccess(data=BalanceOut(
        account_id=new_row[0],
        account_name=new_row[1],
        currency=new_row[2],
        balance=_normalize_amount(new_row[3]),
    ))
