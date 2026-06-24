"""Upsert a brokerage position snapshot into ``asset_holdings``.

Brokerage holdings differ from the crypto/CEX path in three ways, so they
get their own upsert rather than reusing ``wallet_sync.upsert``:

1. Asset class is a real equity class (us_stock / eu_stock / fund / …),
   not ``crypto``; identity is ``(asset_class, symbol)`` with
   ``chain=''`` / ``contract=''`` per the project convention for
   non-crypto assets. IBKR's stable ``conid`` is stored on
   ``Asset.data_source_id`` for disambiguation/auditing.
2. The statement already carries ``markPrice`` + ``currency``, so we write
   ``market_prices`` rows inline — no CoinGecko/yfinance round-trip.
3. No spam filter (broker positions are all legitimate).

Re-sync semantics match the crypto path (per account, ``chain=''``):
    present this round → quantity=<fetched>, is_active=True
    missing this round → quantity=0,         is_active=False
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, AssetHolding, MarketPrice, _utcnow_str, touch_updated_at
from app.services.broker_sync import BrokerPosition, map_asset_class

_BROKER_CHAIN = ""  # non-crypto holdings use the empty-string chain sentinel
_DEFAULT_SOURCE = "ibkr"


async def _get_or_create_asset(
    db: AsyncSession, pos: BrokerPosition, source: str
) -> Asset:
    # Provider may pre-resolve the class (Trade Republic, from ISIN); IBKR
    # leaves it None and we derive it from the category + currency.
    asset_class = pos.asset_class or map_asset_class(pos.asset_category, pos.currency)
    symbol = pos.symbol.strip().upper()

    existing = (
        await db.execute(
            select(Asset).where(
                Asset.asset_class == asset_class,
                Asset.symbol == symbol,
                Asset.chain == "",
                Asset.contract == "",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Backfill the broker id on a row first created elsewhere (e.g. a
        # manual entry) so future audits can map it back to the broker.
        if pos.conid and not existing.data_source_id:
            existing.data_source = source
            existing.data_source_id = pos.conid
            touch_updated_at(existing)
        return existing

    asset = Asset(
        symbol=symbol,
        name=pos.description or symbol,
        asset_class=asset_class,
        currency=pos.currency or "USD",
        data_source=source,
        data_source_id=pos.conid,
        chain="",
        contract="",
    )
    db.add(asset)
    await db.flush()
    return asset


async def apply_broker_snapshot(
    db: AsyncSession,
    account_id: int,
    positions: Iterable[BrokerPosition],
    source: str = _DEFAULT_SOURCE,
) -> int:
    """Apply one fresh brokerage fetch for ``account_id``.

    ``source`` tags the Asset.data_source + MarketPrice.source (e.g. 'ibkr'
    / 'traderepublic'). Returns the number of present positions this
    snapshot (zeroed-out disappeared rows are not counted).
    """
    positions = list(positions)
    now = _utcnow_str()
    present_asset_ids: set[int] = set()
    present_count = 0

    for pos in positions:
        asset = await _get_or_create_asset(db, pos, source)
        present_asset_ids.add(asset.id)
        present_count += 1

        holding = (
            await db.execute(
                select(AssetHolding).where(
                    AssetHolding.account_id == account_id,
                    AssetHolding.asset_id == asset.id,
                    AssetHolding.chain == _BROKER_CHAIN,
                )
            )
        ).scalar_one_or_none()

        if holding is None:
            holding = AssetHolding(
                account_id=account_id,
                asset_id=asset.id,
                chain=_BROKER_CHAIN,
                quantity=pos.quantity,
                avg_cost=pos.avg_cost,
                cost_currency=pos.currency if pos.avg_cost is not None else None,
                is_active=True,
                last_synced_at=now,
            )
            db.add(holding)
        else:
            holding.quantity = pos.quantity
            holding.avg_cost = pos.avg_cost
            holding.cost_currency = pos.currency if pos.avg_cost is not None else None
            holding.is_active = True
            holding.last_synced_at = now
            touch_updated_at(holding)

        # Write the per-position market price straight from the statement.
        # Upsert on (asset_id, source, quoted_at): two syncs in the same
        # second (or a same-second re-sync) must not trip the unique key.
        if pos.mark_price is not None:
            existing_price = (
                await db.execute(
                    select(MarketPrice).where(
                        MarketPrice.asset_id == asset.id,
                        MarketPrice.source == source,
                        MarketPrice.quoted_at == now,
                    )
                )
            ).scalar_one_or_none()
            if existing_price is None:
                db.add(
                    MarketPrice(
                        asset_id=asset.id,
                        quoted_at=now,
                        price=pos.mark_price,
                        currency=pos.currency or "USD",
                        source=source,
                    )
                )
            else:
                existing_price.price = pos.mark_price
                existing_price.currency = pos.currency or "USD"

    # Zero-out anything previously held in this account that didn't appear
    # this round (sold positions). Bulk UPDATE, no N+1.
    reset_filter = [
        AssetHolding.account_id == account_id,
        AssetHolding.chain == _BROKER_CHAIN,
        AssetHolding.is_active == True,  # noqa: E712
    ]
    if present_asset_ids:
        reset_filter.append(AssetHolding.asset_id.notin_(present_asset_ids))
    await db.execute(
        update(AssetHolding)
        .where(*reset_filter)
        .values(
            quantity=Decimal("0"),
            is_active=False,
            last_synced_at=now,
            updated_at=now,
        )
    )

    await db.flush()
    return present_count
