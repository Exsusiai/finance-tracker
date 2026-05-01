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
    """Aggregate cash flow by month, computed from live transactions."""
    stmt = text("""
        SELECT
            substr(occurred_at, 1, 7) AS period,
            SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) AS income,
            SUM(CASE WHEN type = 'expense' THEN ABS(amount) ELSE 0 END) AS expense,
            SUM(CASE WHEN type = 'transfer' THEN ABS(amount) ELSE 0 END) AS transfer,
            SUM(CASE WHEN type = 'income' THEN amount WHEN type = 'expense' THEN amount ELSE 0 END) AS savings
        FROM transactions
        WHERE deleted_at IS NULL
          AND is_pending = 0
          AND (:start IS NULL OR substr(occurred_at, 1, 7) >= :start)
          AND (:end IS NULL OR substr(occurred_at, 1, 7) <= :end)
        GROUP BY period
        ORDER BY period DESC
    """)
    result = await db.execute(stmt, {"start": start_period, "end": end_period})
    rows = result.all()

    monthly_data = []
    for r in rows:
        period = r[0]

        cat_stmt = text("""
            SELECT c.name, c.kind, SUM(t.amount) AS total, COUNT(*) AS cnt
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.deleted_at IS NULL
              AND t.is_pending = 0
              AND substr(t.occurred_at, 1, 7) = :period
              AND t.category_id IS NOT NULL
            GROUP BY t.category_id
            ORDER BY ABS(total) DESC
        """)
        cat_result = await db.execute(cat_stmt, {"period": period})
        cat_rows = cat_result.all()
        by_category = {cr[0]: str(cr[2]) for cr in cat_rows if cr[0]}

        monthly_data.append(CashFlowMonthly(
            period=period,
            income=str(r[1] or Decimal("0")),
            expense=str(r[2] or Decimal("0")),
            transfer=str(r[3] or Decimal("0")),
            savings=str(r[4] or Decimal("0")),
            by_category=by_category,
        ))

    return ApiSuccess(data=monthly_data)


@router.get("/by-category", response_model=ApiSuccess[list[CashFlowByCategory]])
async def cashflow_by_category(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    period: str = Query(..., description="Period in YYYY-MM format"),
):
    """Aggregate spending/income by category for a specific month."""
    stmt = text("""
        SELECT
            c.id,
            COALESCE(c.name, 'Uncategorized'),
            COALESCE(c.kind, 'expense'),
            SUM(t.amount) AS total,
            COUNT(*) AS count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.deleted_at IS NULL
          AND t.is_pending = 0
          AND substr(t.occurred_at, 1, 7) = :period
        GROUP BY t.category_id
        ORDER BY ABS(SUM(t.amount)) DESC
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
    """Income/expense/savings three-line timeseries."""
    stmt = text("""
        SELECT
            substr(occurred_at, 1, 7) AS period,
            SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) AS income,
            SUM(CASE WHEN type = 'expense' THEN ABS(amount) ELSE 0 END) AS expense,
            SUM(CASE WHEN type = 'income' THEN amount WHEN type = 'expense' THEN amount ELSE 0 END) AS savings
        FROM transactions
        WHERE deleted_at IS NULL
          AND is_pending = 0
          AND (:start IS NULL OR substr(occurred_at, 1, 7) >= :start)
          AND (:end IS NULL OR substr(occurred_at, 1, 7) <= :end)
        GROUP BY period
        ORDER BY period ASC
    """)
    result = await db.execute(stmt, {"start": start_period, "end": end_period})
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
    start_year: int | None = Query(None, alias="from_year"),
    start_month: int | None = Query(None, ge=1, le=12, alias="from_month"),
    end_year: int | None = Query(None, alias="to_year"),
    end_month: int | None = Query(None, ge=1, le=12, alias="to_month"),
):
    """Recompute cash flow snapshots for a given range."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_currency = settings.base_currency

    stmt = text("""
        INSERT OR REPLACE INTO cash_flow_snapshots
            (period_year, period_month, base_currency, income_total, expense_total,
             transfer_total, savings_total, other_total, by_category_json, computed_at)
        SELECT
            CAST(substr(occurred_at, 1, 4) AS INTEGER),
            CAST(substr(occurred_at, 6, 2) AS INTEGER),
            :base_currency,
            SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END),
            SUM(CASE WHEN type = 'expense' THEN ABS(amount) ELSE 0 END),
            SUM(CASE WHEN type = 'transfer' THEN ABS(amount) ELSE 0 END),
            SUM(CASE WHEN type = 'income' THEN amount WHEN type = 'expense' THEN amount ELSE 0 END),
            SUM(CASE WHEN type = 'adjustment' THEN amount ELSE 0 END),
            NULL,
            :now
        FROM transactions
        WHERE deleted_at IS NULL AND is_pending = 0
          AND (:from_year IS NULL OR CAST(substr(occurred_at, 1, 4) AS INTEGER) >= :from_year)
          AND (:from_month IS NULL OR CAST(substr(occurred_at, 6, 2) AS INTEGER) >= :from_month)
          AND (:to_year IS NULL OR CAST(substr(occurred_at, 1, 4) AS INTEGER) <= :to_year)
          AND (:to_month IS NULL OR CAST(substr(occurred_at, 6, 2) AS INTEGER) <= :to_month)
        GROUP BY substr(occurred_at, 1, 7)
    """)

    await db.execute(stmt, {
        "base_currency": base_currency,
        "now": now,
        "from_year": start_year,
        "from_month": start_month,
        "to_year": end_year,
        "to_month": end_month,
    })
    await db.flush()

    return ApiSuccess(data={
        "status": "recomputed",
        "base_currency": base_currency,
        "computed_at": now,
    })
