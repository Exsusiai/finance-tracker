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

import structlog
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, AssetHolding, MarketPrice, _utcnow_str, touch_updated_at
from app.services.broker_sync import BrokerPosition, map_asset_class

logger = structlog.get_logger(__name__)

_BROKER_CHAIN = ""  # non-crypto holdings use the empty-string chain sentinel
_DEFAULT_SOURCE = "ibkr"


async def _get_or_create_asset(
    db: AsyncSession, pos: BrokerPosition, source: str
) -> Asset:
    """Resolve (or create) the Asset row for a broker position.

    V7-P2-1: the same ticker can name DIFFERENT securities across markets /
    providers (ADR vs local listing, two ETFs sharing a symbol). Identity used
    to be just (asset_class, symbol, chain='', contract=''), so a second conid
    silently merged into the first row and the two prices overwrote each other.

    Resolution order:
    1. Exact provider-id match (data_source, data_source_id) — authoritative
       even if the displayed symbol changed.
    2. Same (asset_class, symbol) with chain=''/contract='' and an unclaimed or
       matching provider id — reuse + backfill the conid.
    3. Same symbol but a DIFFERENT non-null conid — a genuine identity conflict:
       don't merge. Disambiguate the new security by storing its conid in
       ``contract`` (so the (asset_class, symbol, chain, contract) unique key is
       satisfied) and log it for awareness. Existing rows are left untouched.
    """
    asset_class = pos.asset_class or map_asset_class(pos.asset_category, pos.currency)
    symbol = pos.symbol.strip().upper()
    conid = pos.conid or None

    # 1. Authoritative provider-id match.
    if conid:
        by_id = (
            await db.execute(
                select(Asset).where(
                    Asset.data_source == source,
                    Asset.data_source_id == conid,
                )
            )
        ).scalars().first()
        if by_id is not None:
            return by_id

    # 2. Symbol match on the canonical (chain=''/contract='') row.
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
        if not conid:
            return existing
        if not existing.data_source_id:
            # Unclaimed row (e.g. manual entry) → claim it.
            existing.data_source = source
            existing.data_source_id = conid
            touch_updated_at(existing)
            return existing
        if existing.data_source_id == conid:
            return existing
        # 3. Conflict: same symbol, different conid. Fall through to create a
        # disambiguated row keyed by contract=conid so we never poison the
        # other security's price.
        logger.warning(
            "broker_asset_identity_conflict",
            symbol=symbol,
            asset_class=asset_class,
            existing_id=existing.data_source_id,
            incoming_id=conid,
            source=source,
        )

    asset = Asset(
        symbol=symbol,
        name=pos.description or symbol,
        asset_class=asset_class,
        currency=pos.currency or "USD",
        data_source=source,
        data_source_id=conid,
        chain="",
        # Empty for the normal case; the conid only when disambiguating a
        # same-symbol/different-conid conflict (see step 3).
        contract=conid if (conid and existing is not None) else "",
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

    # V8-P1-1: reclaim holdings that were synced by THIS provider before the
    # `source` column existed (they all defaulted to 'manual' in the migration).
    # Without this, a position sold between syncs never reappears in a fetch, so
    # it would never be claimed and never zeroed — the asset page / net worth
    # would keep counting a sold holding forever. We only touch rows that were
    # previously synced (`last_synced_at IS NOT NULL`, never true for hand-added
    # holdings) AND whose Asset is owned by this provider, so genuine manual
    # holdings stay 'manual'.
    await db.execute(
        update(AssetHolding)
        .where(
            AssetHolding.account_id == account_id,
            AssetHolding.chain == _BROKER_CHAIN,
            AssetHolding.source == "manual",
            AssetHolding.last_synced_at.isnot(None),
            AssetHolding.asset_id.in_(
                select(Asset.id).where(Asset.data_source == source)
            ),
        )
        .values(source=source)
    )

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
                source=source,
                last_synced_at=now,
            )
            db.add(holding)
        else:
            holding.quantity = pos.quantity
            holding.avg_cost = pos.avg_cost
            holding.cost_currency = pos.currency if pos.avg_cost is not None else None
            holding.is_active = True
            # Claim ownership for this provider (covers rows first created
            # manually then later matched by a broker sync).
            holding.source = source
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
    #
    # V7-P1-9: scope the reset to THIS provider's holdings (source == source).
    # Otherwise a user's manually-added holding (source='manual') or another
    # provider's holding in the same brokerage account would be wiped on every
    # sync. conid-less assets first created elsewhere are claimed above when
    # they appear in a fetch, so they migrate to this source naturally.
    reset_filter = [
        AssetHolding.account_id == account_id,
        AssetHolding.chain == _BROKER_CHAIN,
        AssetHolding.source == source,
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
