"""Cash flow aggregation routes — monthly, by-category, timeseries."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.config import get_settings
from app.db import get_db
from app.models import Transaction, Category, CashFlowSnapshot
from app.schemas import (
    ApiSuccess,
    CashFlowByCategory,
    CashFlowMonthly,
    CashFlowTimeseries,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]
settings = get_settings()


@router.get("/monthly", response_model=ApiSuccess[list[CashFlowMonthly]])
async def monthly_cashflow(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    start_period: str | None = Query(None, alias="from", description="Start period YYYY-MM"),
    end_period: str | None = Query(None, alias="to", description="End period YYYY-MM"),
):
    """Aggregate cash flow by month, computed from live transactions.

    Amounts are folded to ``BASE_CURRENCY`` via a CASE expression that:
    1. passes same-currency rows through as-is,
    2. uses ``base_amount`` when set,
    3. applies ``amount * fx_rate_to_base`` when available,
    4. returns NULL (excluded from SUM) when no FX info exists.

    Sprint 4 FIX-19 (§V3-P0-1): foreign-currency rows with no FX data are
    excluded rather than silently added at face value.
    """
    stmt = text("""
        SELECT
            substr(occurred_at, 1, 7) AS period,
            SUM(CASE WHEN type = 'income'  THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END) AS income,
            SUM(CASE WHEN type = 'expense' THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END) AS expense,
            SUM(CASE WHEN type = 'transfer' AND COALESCE(json_valid(metadata_json) AND json_extract(metadata_json, '$.subaccount') = 1, 0) = 0 THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END) AS transfer,
            SUM(CASE WHEN type = 'income'  THEN  ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END)
                     WHEN type = 'expense' THEN -ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END)
                     ELSE 0 END) AS savings,
            COUNT(CASE WHEN currency != :base_currency
                        AND base_amount IS NULL
                        AND fx_rate_to_base IS NULL THEN 1 END) AS fx_missing_count
        FROM transactions
        WHERE deleted_at IS NULL
          AND is_pending = 0
          AND (:start IS NULL OR substr(occurred_at, 1, 7) >= :start)
          AND (:end IS NULL OR substr(occurred_at, 1, 7) <= :end)
        GROUP BY period
        ORDER BY period DESC
    """)
    result = await db.execute(stmt, {"start": start_period, "end": end_period, "base_currency": settings.base_currency})
    rows = result.all()

    monthly_data = []
    for r in rows:
        period = r[0]

        cat_stmt = text("""
            SELECT
                c.name,
                c.kind,
                SUM(ABS(CASE WHEN t.currency = :base_currency THEN t.amount WHEN t.base_amount IS NOT NULL THEN t.base_amount WHEN t.fx_rate_to_base IS NOT NULL THEN t.amount * t.fx_rate_to_base ELSE NULL END)) AS total,
                COUNT(*) AS cnt
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.deleted_at IS NULL
              AND t.is_pending = 0
              AND substr(t.occurred_at, 1, 7) = :period
              AND t.category_id IS NOT NULL
            GROUP BY t.category_id
            ORDER BY total DESC
        """)
        cat_result = await db.execute(cat_stmt, {"period": period, "base_currency": settings.base_currency})
        cat_rows = cat_result.all()
        by_category = {cr[0]: str(cr[2]) for cr in cat_rows if cr[0]}

        monthly_data.append(CashFlowMonthly(
            period=period,
            base_currency=settings.base_currency,
            income=str(r[1] or Decimal("0")),
            expense=str(r[2] or Decimal("0")),
            transfer=str(r[3] or Decimal("0")),
            savings=str(r[4] or Decimal("0")),
            fx_missing_count=int(r[5] or 0),
            by_category=by_category,
        ))

    return ApiSuccess(data=monthly_data)


@router.get("/by-category", response_model=ApiSuccess[list[CashFlowByCategory]])
async def cashflow_by_category(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    period: str = Query(..., description="Period in YYYY-MM format"),
):
    """Aggregate spending/income by category for a specific month (folded to BASE_CURRENCY)."""
    stmt = text("""
        SELECT
            c.id,
            COALESCE(c.name, 'Uncategorized'),
            COALESCE(c.kind, 'expense'),
            SUM(ABS(COALESCE(t.base_amount, t.amount * t.fx_rate_to_base, t.amount))) AS total,
            COUNT(*) AS count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.deleted_at IS NULL
          AND t.is_pending = 0
          AND substr(t.occurred_at, 1, 7) = :period
        GROUP BY t.category_id
        ORDER BY total DESC
    """)
    result = await db.execute(stmt, {"period": period})
    rows = result.all()

    return ApiSuccess(data=[
        CashFlowByCategory(
            category_id=r[0],
            category_name=r[1],
            kind=r[2],
            total=str(r[3] or Decimal("0")),
            count=r[4] or 0,
        )
        for r in rows
    ])


@router.get("/timeseries", response_model=ApiSuccess[CashFlowTimeseries])
async def cashflow_timeseries(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    start_period: str | None = Query(None, alias="from", description="Start period YYYY-MM"),
    end_period: str | None = Query(None, alias="to", description="End period YYYY-MM"),
):
    """Income/expense/savings three-line timeseries (folded to BASE_CURRENCY).

    Sprint 4 FIX-19 (§V3-P0-1): foreign-currency rows with no FX data are
    excluded (NULL) rather than mixed in at face value.
    """
    stmt = text("""
        SELECT
            substr(occurred_at, 1, 7) AS period,
            SUM(CASE WHEN type = 'income'  THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END) AS income,
            SUM(CASE WHEN type = 'expense' THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END) AS expense,
            SUM(CASE WHEN type = 'income'  THEN  ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END)
                     WHEN type = 'expense' THEN -ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END)
                     ELSE 0 END) AS savings
        FROM transactions
        WHERE deleted_at IS NULL
          AND is_pending = 0
          AND (:start IS NULL OR substr(occurred_at, 1, 7) >= :start)
          AND (:end IS NULL OR substr(occurred_at, 1, 7) <= :end)
        GROUP BY period
        ORDER BY period ASC
    """)
    result = await db.execute(stmt, {"start": start_period, "end": end_period, "base_currency": settings.base_currency})
    rows = result.all()

    periods = []
    income = []
    expense = []
    savings = []

    for r in rows:
        periods.append(r[0])
        income.append(str(r[1] or Decimal("0")))
        expense.append(str(r[2] or Decimal("0")))
        savings.append(str(r[3] or Decimal("0")))

    return ApiSuccess(data=CashFlowTimeseries(
        periods=periods,
        income=income,
        expense=expense,
        savings=savings,
    ))


@router.post("/recompute", response_model=ApiSuccess[dict])
async def recompute_cashflow(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    from_period: str | None = Query(None, alias="from", description="Start period YYYY-MM (canonical)"),
    to_period: str | None = Query(None, alias="to", description="End period YYYY-MM (canonical)"),
    start_year: int | None = Query(None, alias="from_year", description="Legacy: start year"),
    start_month: int | None = Query(None, ge=1, le=12, alias="from_month", description="Legacy: start month"),
    end_year: int | None = Query(None, alias="to_year", description="Legacy: end year"),
    end_month: int | None = Query(None, ge=1, le=12, alias="to_month", description="Legacy: end month"),
):
    """Recompute cash flow snapshots for a given range.

    Canonical params: ``from=YYYY-MM`` / ``to=YYYY-MM`` (FIX-21: correctly
    handles cross-year ranges). Legacy ``from_year``/``from_month``/
    ``to_year``/``to_month`` params are still accepted for backward compat;
    canonical params take precedence when both are present.

    Sprint 4 FIX-19 (§V3-P0-1): foreign-currency rows with no FX data are
    excluded rather than added at face value.
    Sprint 4 FIX-21 (§V3-P1-4): cross-year range filter uses string
    comparison on ``substr(occurred_at, 1, 7)`` instead of independent
    year/month comparisons that broke cross-year queries.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_currency = settings.base_currency

    # Build canonical period strings — new params take precedence over legacy.
    effective_from: str | None = from_period
    effective_to: str | None = to_period
    if effective_from is None and start_year is not None and start_month is not None:
        effective_from = f"{start_year:04d}-{start_month:02d}"
    if effective_to is None and end_year is not None and end_month is not None:
        effective_to = f"{end_year:04d}-{end_month:02d}"

    stmt = text("""
        INSERT OR REPLACE INTO cash_flow_snapshots
            (period_year, period_month, base_currency, income_total, expense_total,
             transfer_total, savings_total, other_total, by_category_json, computed_at)
        SELECT
            CAST(substr(occurred_at, 1, 4) AS INTEGER),
            CAST(substr(occurred_at, 6, 2) AS INTEGER),
            :base_currency,
            SUM(CASE WHEN type = 'income'  THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END),
            SUM(CASE WHEN type = 'expense' THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END),
            SUM(CASE WHEN type = 'transfer' AND COALESCE(json_valid(metadata_json) AND json_extract(metadata_json, '$.subaccount') = 1, 0) = 0 THEN ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END) ELSE 0 END),
            SUM(CASE WHEN type = 'income'  THEN  ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END)
                     WHEN type = 'expense' THEN -ABS(CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END)
                     ELSE 0 END),
            SUM(CASE WHEN type = 'adjustment' THEN CASE WHEN currency = :base_currency THEN amount WHEN base_amount IS NOT NULL THEN base_amount WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base ELSE NULL END ELSE 0 END),
            NULL,
            :now
        FROM transactions
        WHERE deleted_at IS NULL AND is_pending = 0
          AND (:from_period IS NULL OR substr(occurred_at, 1, 7) >= :from_period)
          AND (:to_period   IS NULL OR substr(occurred_at, 1, 7) <= :to_period)
        GROUP BY substr(occurred_at, 1, 7)
    """)

    result = await db.execute(stmt, {
        "base_currency": base_currency,
        "now": now,
        "from_period": effective_from,
        "to_period": effective_to,
    })
    await db.flush()

    # Count fx_missing rows in the recomputed range
    fx_count_stmt = text("""
        SELECT COUNT(*) FROM transactions
        WHERE deleted_at IS NULL AND is_pending = 0
          AND currency != :base_currency
          AND base_amount IS NULL
          AND fx_rate_to_base IS NULL
          AND (:from_period IS NULL OR substr(occurred_at, 1, 7) >= :from_period)
          AND (:to_period   IS NULL OR substr(occurred_at, 1, 7) <= :to_period)
    """)
    fx_missing_count = (await db.execute(fx_count_stmt, {
        "base_currency": base_currency,
        "from_period": effective_from,
        "to_period": effective_to,
    })).scalar() or 0

    return ApiSuccess(data={
        "status": "recomputed",
        "base_currency": base_currency,
        "computed_at": now,
        "fx_missing_count": fx_missing_count,
    })
