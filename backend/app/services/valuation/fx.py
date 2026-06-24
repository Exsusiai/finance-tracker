"""Currency conversion to the base currency via stored FX rates.

Extracted from ``api/v1/holdings.py`` (the valuation/engine.py docstring
explicitly sanctions this) so both the holdings endpoints and the
account-balance aggregation share one correct implementation.

Strategy: same-currency → direct rate → inverse rate → triangulate via a
pivot (CNY / USD / EUR). USD-pegged stablecoins are aliased to USD so
crypto holdings priced in USDT/USDC/etc. resolve through the fiat FX table.
Returns ``None`` when no path exists.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FxRate

_USD_PEGGED = frozenset({"USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX"})


async def latest_fx_rate(db: AsyncSession, base: str, quote: str) -> Decimal | None:
    """Return the newest ``FxRate.rate`` for (base → quote), or ``None``."""
    stmt = (
        select(FxRate)
        .where(FxRate.base_currency == base, FxRate.quote_currency == quote)
        .order_by(FxRate.quoted_at.desc())
        .limit(1)
    )
    fx = (await db.execute(stmt)).scalar_one_or_none()
    return fx.rate if fx else None


async def convert_to_base(
    db: AsyncSession,
    amount: Decimal,
    src_currency: str,
    base_currency: str,
) -> Decimal | None:
    """Convert ``amount`` from ``src_currency`` → ``base_currency``.

    Returns ``None`` when no FX path is available.
    """
    if src_currency in _USD_PEGGED:
        src_currency = "USD"
    if base_currency in _USD_PEGGED:
        base_currency = "USD"

    if src_currency == base_currency:
        return amount

    direct = await latest_fx_rate(db, src_currency, base_currency)
    if direct is not None and direct > 0:
        return amount * direct

    inverse = await latest_fx_rate(db, base_currency, src_currency)
    if inverse is not None and inverse > 0:
        return amount / inverse

    # Triangulate via a pivot currency. CNY is critical because the live FX
    # scheduler emits everything keyed base_currency='CNY'.
    for pivot in ("CNY", "USD", "EUR"):
        if pivot in (src_currency, base_currency):
            continue
        a_direct = await latest_fx_rate(db, src_currency, pivot)
        a_inverse = (
            await latest_fx_rate(db, pivot, src_currency) if a_direct is None else None
        )
        a = a_direct if a_direct is not None else (
            (Decimal(1) / a_inverse) if (a_inverse is not None and a_inverse > 0) else None
        )
        b_direct = await latest_fx_rate(db, pivot, base_currency)
        b_inverse = (
            await latest_fx_rate(db, base_currency, pivot) if b_direct is None else None
        )
        b = b_direct if b_direct is not None else (
            (Decimal(1) / b_inverse) if (b_inverse is not None and b_inverse > 0) else None
        )
        if a is not None and b is not None:
            return amount * a * b

    return None
