"""Valuation service — asset portfolio valuation helpers."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetHolding, Asset, MarketPrice, FxRate


async def compute_holding_value(
    db: AsyncSession,
    holding: AssetHolding,
    base_currency: str = "CNY",
) -> Decimal | None:
    """Compute the base-currency value of a single holding."""
    # Get latest price
    price_stmt = (
        select(MarketPrice)
        .where(MarketPrice.asset_id == holding.asset_id)
        .order_by(MarketPrice.quoted_at.desc())
        .limit(1)
    )
    result = await db.execute(price_stmt)
    latest = result.scalar_one_or_none()
    if not latest:
        return None

    value = holding.quantity * latest.price

    if latest.currency == base_currency:
        return value

    # Convert via FX
    fx_stmt = (
        select(FxRate)
        .where(
            FxRate.base_currency == base_currency,
            FxRate.quote_currency == latest.currency,
        )
        .order_by(FxRate.quoted_at.desc())
        .limit(1)
    )
    fx_result = await db.execute(fx_stmt)
    fx = fx_result.scalar_one_or_none()
    if fx:
        return value * fx.rate
    return None
