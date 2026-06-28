"""Balance anchoring + statement reconciliation.

Two related operations:

- **Anchor**: set ``account.initial_balance`` so the ledger balance at a known
  date equals a known-true balance — ``initial = true_balance − Σ signed tx
  (≤ as_of)``. Because we subtract recorded transactions up to ``as_of``, this
  is race-free: it can run any time, and transactions after ``as_of`` stack on
  top correctly. No fake adjustment row, and the WHOLE historical curve shifts
  to reality (unlike adjust-balance, which only fixes forward).

- **Reconcile**: compare a statement's printed closing balance against the
  computed ledger balance at the statement's last transaction date. A non-zero
  discrepancy on an already-anchored account flags a book-keeping problem
  (missing / duplicate / wrong-amount rows).
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, _utcnow_str, touch_updated_at
from app.services.valuation.cash_history import _SIGNED_AMOUNT


async def signed_tx_sum(db: AsyncSession, account_id: int, as_of: str) -> Decimal:
    """Σ signed transaction amounts (account currency) with occurred_at ≤ as_of.

    Mirrors v_account_balance's per-row sign logic (ignores sub-account moves,
    includes pending). So ``initial_balance + signed_tx_sum`` == the account's
    balance at ``as_of``.
    """
    row = (await db.execute(text(f"""
        SELECT COALESCE(SUM({_SIGNED_AMOUNT}), 0)
        FROM transactions t
        WHERE t.account_id = :aid
          AND t.deleted_at IS NULL
          AND t.occurred_at <= :as_of
    """), {"aid": account_id, "as_of": as_of})).scalar()
    return Decimal(str(row or 0))


def normalize_closing(account_type: str, closing_raw: Decimal) -> Decimal:
    """Map a statement's printed closing balance to the ledger sign convention.

    Credit cards store debt as a NEGATIVE balance, but statements print the
    amount owed (advanzia/AMEX positive, TFBank already negative) → normalise to
    ``−abs``. Asset accounts (bank/cash) use the printed sign as-is.
    """
    if account_type == "credit_card":
        return -abs(closing_raw)
    return closing_raw


def _is_anchored(account: Account) -> bool:
    """Whether the account was previously anchored (has a balance_anchor)."""
    if not account.metadata_json:
        return False
    try:
        return "balance_anchor" in (json.loads(account.metadata_json) or {})
    except (json.JSONDecodeError, TypeError):
        return False


async def compute_reconciliation(
    db: AsyncSession, account: Account, closing_raw: Decimal, as_of: str
) -> dict:
    """Compare statement closing vs computed ledger balance at ``as_of``.

    ``previously_anchored`` lets the UI word the prompt correctly: a non-zero
    discrepancy on an ALREADY-anchored account signals book-keeping drift
    (missing / duplicate / wrong rows); on a never-anchored one it's just the
    unrecorded opening (first-time calibration).
    """
    closing = normalize_closing(account.type, closing_raw)
    computed = account.initial_balance + await signed_tx_sum(db, account.id, as_of)
    return {
        "closing_balance": str(closing),
        "computed_balance": str(computed),
        "discrepancy": str(closing - computed),
        "currency": account.currency,
        "as_of": as_of,
        "previously_anchored": _is_anchored(account),
    }


async def anchor_account_balance(
    db: AsyncSession, account: Account, balance: Decimal | str, as_of: str
) -> Decimal:
    """Set ``initial_balance`` so the ledger balance at ``as_of`` equals
    ``balance`` (ledger sign convention). Records the anchor in metadata."""
    target = Decimal(str(balance))
    new_initial = target - await signed_tx_sum(db, account.id, as_of)
    account.initial_balance = new_initial

    meta: dict = {}
    if account.metadata_json:
        try:
            meta = json.loads(account.metadata_json)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    meta["balance_anchor"] = {
        "as_of": as_of,
        "balance": str(target),
        "set_at": _utcnow_str(),
    }
    account.metadata_json = json.dumps(meta)
    touch_updated_at(account)
    return new_initial
