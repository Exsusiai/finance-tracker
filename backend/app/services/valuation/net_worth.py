"""Single source for net-worth aggregation: cash (account balances) +
investments (holdings × latest price), folded to BASE_CURRENCY.

Used by ``GET /holdings/portfolio/net-worth`` AND the monthly portfolio
snapshot job, so both agree exactly (money math is single-sourced here, like
``paired_dedup_predicate`` / ``_AMOUNT_BASE_EXPR`` elsewhere).

Honours per-account ``include_in_total``; snapshot accounts (brokerage /
crypto_wallet / exchange) contribute via the investments leg only — their cash
ledger is excluded so a stray initial_balance / adjustment isn't double-counted
on top of the holdings value (review V7 §P1-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Asset, AssetHolding, MarketPrice
from app.services.valuation.fx import convert_to_base


@dataclass
class NetWorthResult:
    base_currency: str
    cash_total: Decimal
    investment_total: Decimal
    cash_by_currency: dict[str, dict[str, str]]
    investment_by_currency: dict[str, dict[str, Decimal]]
    as_of: str

    @property
    def net_worth(self) -> Decimal:
        return self.cash_total + self.investment_total


async def compute_net_worth(db: AsyncSession, base_currency: str) -> NetWorthResult:
    # 1. Cash: account balances grouped by currency, opted-out + snapshot
    # accounts excluded.
    balances_stmt = text("""
        SELECT v.currency, SUM(v.balance) AS total
        FROM v_account_balance v
        JOIN accounts a ON a.id = v.account_id
        WHERE a.include_in_total = 1 AND a.deleted_at IS NULL
          AND a.type NOT IN ('brokerage', 'crypto_wallet', 'exchange')
        GROUP BY v.currency
    """)
    balances_result = await db.execute(balances_stmt)
    cash_total = Decimal("0")
    cash_details: dict[str, dict[str, str]] = {}
    for currency, total in balances_result.all():
        original = total if isinstance(total, Decimal) else Decimal(str(total or 0))
        converted = await convert_to_base(db, original, currency, base_currency)
        if converted is not None:
            cash_total += converted
            cash_details[currency] = {"original": str(original), "converted": str(converted)}
        else:
            cash_details[currency] = {"original": str(original), "converted": ""}

    # 2. Investments: holdings × latest price, skip rows missing price/FX.
    inv_stmt = (
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
        .join(Account, Account.id == AssetHolding.account_id)
        .where(
            Account.include_in_total == True,  # noqa: E712
            Account.deleted_at.is_(None),
            AssetHolding.is_active == True,  # noqa: E712
        )
    )
    inv_result = await db.execute(inv_stmt)
    investment_total = Decimal("0")
    investment_by_currency: dict[str, dict[str, Decimal]] = {}
    for holding, asset in inv_result.all():
        price_stmt = (
            select(MarketPrice)
            .where(MarketPrice.asset_id == asset.id)
            .order_by(MarketPrice.quoted_at.desc())
            .limit(1)
        )
        latest = (await db.execute(price_stmt)).scalar_one_or_none()
        if latest is None:
            continue
        original_value = holding.quantity * latest.price
        bucket = investment_by_currency.setdefault(
            latest.currency,
            {"original_value": Decimal("0"), "base_value": Decimal("0")},
        )
        bucket["original_value"] += original_value
        if latest.currency == base_currency:
            converted = original_value
        else:
            converted = await convert_to_base(db, original_value, latest.currency, base_currency)
        if converted is None:
            continue
        bucket["base_value"] += converted
        investment_total += converted

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return NetWorthResult(
        base_currency=base_currency,
        cash_total=cash_total,
        investment_total=investment_total,
        cash_by_currency=cash_details,
        investment_by_currency=investment_by_currency,
        as_of=now,
    )
