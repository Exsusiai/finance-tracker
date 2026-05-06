"""Cashflow snapshot recompute service.

Single-period and multi-period helpers — used both by the manual
`POST /api/v1/cashflow/recompute` endpoint and by transaction CRUD hooks
that auto-refresh after every mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

logger = structlog.get_logger(__name__)
_settings = get_settings()


def parse_period(occurred_at: str | None) -> tuple[int, int] | None:
    """Pull (year, month) out of `YYYY-MM-...` strings. Returns None if unparsable."""
    if not occurred_at or len(occurred_at) < 7:
        return None
    try:
        return int(occurred_at[0:4]), int(occurred_at[5:7])
    except ValueError:
        return None


# FIX-3 (multi-currency): use base_amount when available, else apply
# fx_rate_to_base, else fall back to raw amount (best effort — same-currency
# rows or rows where the parser couldn't resolve FX).
# FIX-2 (savings): savings = ABS(income) − ABS(expense). Previously the
# formula summed `amount` for both income AND expense, which (because amount
# is stored as a positive magnitude) added expense to savings instead of
# subtracting it.
_AMOUNT_BASE_EXPR = "COALESCE(base_amount, amount * fx_rate_to_base, amount)"

_RECOMPUTE_SQL = text(f"""
    INSERT OR REPLACE INTO cash_flow_snapshots
        (period_year, period_month, base_currency,
         income_total, expense_total, transfer_total, savings_total, other_total,
         by_category_json, by_account_json, computed_at)
    SELECT
        :year, :month, :base_currency,
        COALESCE(SUM(CASE WHEN type = 'income'  THEN ABS({_AMOUNT_BASE_EXPR}) ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'expense' THEN ABS({_AMOUNT_BASE_EXPR}) ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'transfer' THEN ABS({_AMOUNT_BASE_EXPR}) ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'income'  THEN  ABS({_AMOUNT_BASE_EXPR})
                          WHEN type = 'expense' THEN -ABS({_AMOUNT_BASE_EXPR})
                          ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'adjustment' THEN {_AMOUNT_BASE_EXPR} ELSE 0 END), 0),
        NULL,
        NULL,
        :now
    FROM transactions
    WHERE deleted_at IS NULL
      AND is_pending = 0
      AND CAST(substr(occurred_at, 1, 4) AS INTEGER) = :year
      AND CAST(substr(occurred_at, 6, 2) AS INTEGER) = :month
""")


async def recompute_period(db: AsyncSession, year: int, month: int) -> None:
    """Recompute the snapshot for a single (year, month). Caller commits."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(_RECOMPUTE_SQL, {
        "year": year,
        "month": month,
        "base_currency": _settings.base_currency,
        "now": now,
    })


async def recompute_for_periods(
    db: AsyncSession,
    periods: Iterable[tuple[int, int] | None],
) -> int:
    """Deduplicate and recompute multiple periods in one call. Returns count run."""
    unique = {p for p in periods if p is not None}
    for y, m in unique:
        try:
            await recompute_period(db, y, m)
        except Exception as e:
            logger.warning("cashflow_recompute_failed", year=y, month=m, error=str(e))
    return len(unique)
