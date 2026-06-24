"""Aggregate ``asset_holdings × latest market price`` per account.

Used by the /accounts/balances endpoint to surface the real worth of
crypto_wallet / exchange accounts — for those, the SQL
``v_account_balance`` view is always 0 because they hold no
transactions, only holdings.

Returns USDT values (the unit of `market_prices` rows written by the
wallet_sync pipeline). Already-priced rows on other vs_currencies are
ignored — the latest USDT row wins.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


# Latest-USDT price per asset. SQLite supports correlated subqueries
# cheaply for our small dataset; no need for a CTE / window function.
#
# JOIN accounts + filter on deleted_at IS NULL so soft-deleted crypto /
# exchange accounts don't leak into the net_worth total (Py-HIGH
# finding 2026-05-19). The `account_ids` filter callers pass *can* be
# an unfiltered list (e.g. all crypto accounts) — this defensive
# JOIN ensures the helper is safe regardless.
_SQL = """
SELECT
    h.account_id      AS account_id,
    SUM(h.quantity * (
        SELECT mp.price
        FROM market_prices mp
        WHERE mp.asset_id = h.asset_id
          AND mp.currency = 'USDT'
        ORDER BY mp.quoted_at DESC
        LIMIT 1
    )) AS value
FROM asset_holdings h
JOIN accounts a ON a.id = h.account_id
WHERE h.is_active = 1
  AND a.deleted_at IS NULL
{account_filter}
GROUP BY h.account_id
"""


async def compute_holdings_value_per_account(
    db: AsyncSession,
    account_ids: Iterable[int] | None = None,
) -> dict[int, Decimal]:
    """Return ``{account_id → USDT value}`` for active holdings.

    Accounts whose every holding lacks a USDT price are omitted from
    the result (SUM(NULL) = NULL → empty).
    """
    account_filter = ""
    params: dict = {}
    if account_ids is not None:
        ids = list(account_ids)
        if not ids:
            return {}
        # Build positional placeholders since `IN :tuple` needs special
        # treatment with text().
        placeholders = ",".join(f":a{i}" for i in range(len(ids)))
        account_filter = f"AND h.account_id IN ({placeholders})"
        params = {f"a{i}": v for i, v in enumerate(ids)}

    rows = (
        await db.execute(text(_SQL.format(account_filter=account_filter)), params)
    ).all()

    out: dict[int, Decimal] = {}
    for account_id, value in rows:
        if value is None:
            continue
        out[int(account_id)] = Decimal(str(value))
    return out


async def compute_brokerage_value_per_account(
    db: AsyncSession,
    account_ids: Iterable[int],
    base_currency: str,
) -> dict[int, Decimal]:
    """Return ``{account_id → base-currency value}`` for brokerage holdings.

    Unlike the crypto/CEX path (everything quoted in USDT), brokerage
    positions are priced in their native currency (USD / EUR / …), so each
    holding's value is converted to ``base_currency`` via the shared FX
    helper. Holdings whose currency has no FX path are skipped (omitted
    from the sum) — the same lenient behaviour as net_worth.
    """
    from app.models import Asset, AssetHolding, MarketPrice
    from app.services.valuation.fx import convert_to_base

    ids = list(account_ids)
    if not ids:
        return {}

    rows = (
        await db.execute(
            select(AssetHolding, Asset)
            .join(Asset, Asset.id == AssetHolding.asset_id)
            .where(
                AssetHolding.account_id.in_(ids),
                AssetHolding.is_active == True,  # noqa: E712
            )
        )
    ).all()

    out: dict[int, Decimal] = {}
    for holding, asset in rows:
        latest = (
            await db.execute(
                select(MarketPrice)
                .where(MarketPrice.asset_id == asset.id)
                .order_by(MarketPrice.quoted_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None:
            continue
        original = holding.quantity * latest.price
        converted = await convert_to_base(db, original, latest.currency, base_currency)
        if converted is None:
            continue
        out[holding.account_id] = out.get(holding.account_id, Decimal("0")) + converted
    return out
