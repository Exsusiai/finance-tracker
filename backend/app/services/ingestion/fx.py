"""FX rate resolution for multi-currency ingestion (FIX-13 / review V2 §V2-P0-1).

Async counterpart to the sync `_convert_fx` in mcp-server/src/finance_mcp/server.py.
Same triangulation strategy: direct → inverse → CNY/USD/EUR pivots.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FxRate

_PIVOTS = ("CNY", "USD", "EUR")


async def _fx_row(db: AsyncSession, base: str, quote: str) -> Decimal | None:
    """Return the latest rate for (base→quote) or None."""
    result = await db.execute(
        select(FxRate.rate)
        .where(FxRate.base_currency == base, FxRate.quote_currency == quote)
        .order_by(FxRate.quoted_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return Decimal(str(row)) if row is not None else None


async def resolve_fx_to_base(
    db: AsyncSession,
    *,
    src_currency: str,
    base_currency: str,
) -> Decimal | None:
    """Return the multiplier so that ``amount_in_src * rate = amount_in_base``.

    Returns None if no rate path exists.

    Strategy (mirrors mcp-server _convert_fx):
      1. Same currency → 1
      2. Direct rate (src → base)
      3. Inverse rate (base → src)
      4. Triangulate via CNY, USD, EUR pivots (skip pivot if it equals src or base)
    """
    if not src_currency or src_currency == base_currency:
        return Decimal("1")

    # 1. Direct
    direct = await _fx_row(db, src_currency, base_currency)
    if direct is not None:
        return direct

    # 2. Inverse
    inverse = await _fx_row(db, base_currency, src_currency)
    if inverse is not None and inverse > 0:
        return Decimal("1") / inverse

    # 3. Triangulate
    for pivot in _PIVOTS:
        if pivot in (src_currency, base_currency):
            continue
        a = await _fx_row(db, src_currency, pivot)
        if a is None:
            a_inv = await _fx_row(db, pivot, src_currency)
            if a_inv is not None and a_inv > 0:
                a = Decimal("1") / a_inv
        if a is None:
            continue
        b = await _fx_row(db, pivot, base_currency)
        if b is None:
            b_inv = await _fx_row(db, base_currency, pivot)
            if b_inv is not None and b_inv > 0:
                b = Decimal("1") / b_inv
        if b is None:
            continue
        return a * b

    return None
