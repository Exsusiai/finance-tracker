"""Single source of the transaction write-entrance invariants (V9-R1).

Every path that creates or moves a Transaction — REST create / batch / PATCH,
inbox confirm, PDF/CSV import, MCP tools — must enforce the same three rules:

1. **kind match**: an assigned category's kind must equal the transaction
   type (Sprint 1 FIX-5), or dashboards silently hide rows.
2. **no snapshot-account ledger**: brokerage / crypto_wallet / exchange
   accounts are valued from ``asset_holdings × market_prices`` (V8-P1-3);
   a cash row there double-counts in ``/accounts/balances``.
3. **currency unit match**: ``v_account_balance`` sums raw amounts in the
   account's own unit (V9-F04); a 100 USD row on a EUR account silently
   debits 100 EUR.

These previously lived inside ``api/v1/transactions.py`` where only the REST
routes could reach them — the review's recurring theme was entrances drifting
apart. Import from here; do not re-implement.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.models import Account, Category

# Account types whose balance is derived from auto-synced holdings snapshots,
# never from transaction rows. Mirrors the guard rationale in
# services/wallet_sync + /accounts/balances.
SNAPSHOT_ACCOUNT_TYPES = frozenset({"brokerage", "crypto_wallet", "exchange"})


async def validate_kind_match(
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


async def reject_snapshot_account(db: AsyncSession, account_id: int) -> None:
    """V8-P1-3: refuse to create a normal transaction on a snapshot account
    (brokerage / crypto_wallet / exchange). Those accounts are valued from
    ``asset_holdings × market_prices``; a transaction would inject a phantom
    cash ledger that ``/accounts/balances`` adds on top of the holdings value.
    Their real movements come from sync, not hand-entered transactions.
    """
    acc = (await db.execute(
        select(Account.type).where(Account.id == account_id, Account.deleted_at.is_(None))
    )).scalar_one_or_none()
    if acc in SNAPSHOT_ACCOUNT_TYPES:
        raise InvalidInputError(
            f"{acc} accounts are valued from holdings — they cannot hold "
            "transactions. Sync the account or edit its holdings instead.",
            details={"account_id": account_id, "account_type": acc},
        )


async def validate_account_currency(
    db: AsyncSession, account_id: int, currency: str | None
) -> None:
    """V9-F04: a transaction's currency must equal its account's currency.

    ``v_account_balance`` sums raw ``amount`` in the account's own unit — a
    100 USD row on a EUR account silently debits 100 EUR. The account model
    is single-currency, so every write entrance rejects the mismatch instead
    of mixing units. (``base_amount`` is the global base fold, NOT an
    account-unit amount.)
    """
    if not currency:
        return
    acc_currency = (await db.execute(
        select(Account.currency).where(
            Account.id == account_id, Account.deleted_at.is_(None)
        )
    )).scalar_one_or_none()
    if acc_currency is not None and acc_currency.upper() != currency.upper():
        raise InvalidInputError(
            f"交易币种 {currency} 与账户币种 {acc_currency} 不一致。"
            "账户为单币种模型；请换用对应币种的账户，或修正交易币种。",
            details={
                "account_id": account_id,
                "account_currency": acc_currency,
                "transaction_currency": currency,
            },
        )
