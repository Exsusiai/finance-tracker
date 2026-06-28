"""Forward-capture of WEEKLY portfolio value snapshots.

Historical portfolio value can't be reconstructed (holdings store only
current quantities), so we record going forward: upsert the CURRENT week's
row (keyed by that week's Monday, "YYYY-MM-DD") with the latest valuation.
Re-running within the same week overwrites it; at week rollover a fresh row
starts, freezing the prior week at its last-captured value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSnapshot
from app.services.valuation.net_worth import compute_net_worth


def _current_week_period() -> str:
    """Monday of the current ISO week as 'YYYY-MM-DD' — sortable, one per week."""
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")


async def capture_portfolio_snapshot(db: AsyncSession, base_currency: str) -> PortfolioSnapshot:
    r = await compute_net_worth(db, base_currency)
    period = _current_week_period()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    existing = (
        await db.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.period == period))
    ).scalar_one_or_none()

    if existing is not None:
        existing.base_currency = r.base_currency
        existing.cash_total = r.cash_total
        existing.investment_total = r.investment_total
        existing.net_worth = r.net_worth
        existing.captured_at = now
        snap = existing
    else:
        snap = PortfolioSnapshot(
            period=period,
            base_currency=r.base_currency,
            cash_total=r.cash_total,
            investment_total=r.investment_total,
            net_worth=r.net_worth,
            captured_at=now,
        )
        db.add(snap)
    await db.flush()
    return snap
