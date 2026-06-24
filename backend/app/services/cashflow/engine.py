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
# Sprint 4 FIX-19 (review V3 §V3-P0-1): no longer fall back to raw `amount`
# when both base_amount and fx_rate_to_base are NULL — that silently mixed
# foreign currencies. Same-currency rows (currency = base_currency) bypass
# the FX path; everything else needs an explicit FX field, otherwise the
# row is NULL and excluded from the SUM.
_AMOUNT_BASE_EXPR = (
    "CASE "
    "  WHEN currency = :base_currency THEN amount "
    "  WHEN base_amount IS NOT NULL THEN base_amount "
    "  WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base "
    "  ELSE NULL "
    "END"
)

# Sprint 4 audit (2026-05-06): sub-account transfers (in-bank moves like
# N26 main → Saving Space, marked metadata.subaccount=true) shouldn't
# inflate transfer_total either — they're invisible noise from the user's
# whole-portfolio perspective. The COALESCE wrap handles SQLite NULL
# three-valued logic: a row is "subaccount" only when metadata_json is
# valid JSON AND $.subaccount = 1; everything else (NULL metadata,
# malformed JSON, missing key) is treated as not-subaccount.
_NOT_SUBACCOUNT = (
    "COALESCE("
    "  json_valid(metadata_json) "
    "  AND json_extract(metadata_json, '$.subaccount') = 1, "
    "  0"
    ") = 0"
)

_RECOMPUTE_SQL = text(f"""
    INSERT OR REPLACE INTO cash_flow_snapshots
        (period_year, period_month, base_currency,
         income_total, expense_total, transfer_total, savings_total, other_total,
         by_category_json, by_account_json, computed_at)
    SELECT
        :year, :month, :base_currency,
        COALESCE(SUM(CASE WHEN type = 'income'  THEN ABS({_AMOUNT_BASE_EXPR}) ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'expense' THEN ABS({_AMOUNT_BASE_EXPR}) ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'transfer' AND {_NOT_SUBACCOUNT}
                          THEN ABS({_AMOUNT_BASE_EXPR}) ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'income'  THEN  ABS({_AMOUNT_BASE_EXPR})
                          WHEN type = 'expense' THEN -ABS({_AMOUNT_BASE_EXPR})
                          ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN type = 'adjustment' THEN {_AMOUNT_BASE_EXPR} ELSE 0 END), 0),
        NULL,
        NULL,
        :now
    FROM transactions t
    WHERE t.deleted_at IS NULL
      AND t.is_pending = 0
      AND CAST(substr(t.occurred_at, 1, 4) AS INTEGER) = :year
      AND CAST(substr(t.occurred_at, 6, 2) AS INTEGER) = :month
      -- A transfer is ONE event recorded as TWO legs; both are type='transfer'
      -- so summing both double-counts transfer_total. Drop a leg when its
      -- paired partner is live AND has a smaller id (keeps one leg per pair).
      -- Only paired transfers match, so income/expense/adjustment/savings are
      -- untouched. Mirrors the dedup in api/v1/cashflow.py::cashflow_by_category.
      AND NOT EXISTS (
          SELECT 1 FROM transactions p
          WHERE p.deleted_at IS NULL
            AND p.id < t.id
            AND t.metadata_json IS NOT NULL
            AND json_valid(t.metadata_json)
            AND p.id = json_extract(t.metadata_json, '$.paired_with_tx_id')
      )
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
