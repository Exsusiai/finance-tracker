"""Asset holdings routes — CRUD + portfolio summary/breakdown."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db import get_db
from app.models import AssetHolding, Asset, MarketPrice, FxRate
from app.models import touch_updated_at
from app.schemas import (
    ApiSuccess,
    HoldingCreate,
    HoldingOut,
    HoldingUpdate,
    PortfolioBreakdown,
    PortfolioSummary,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]
settings = get_settings()


def _holding_to_out(
    h: AssetHolding,
    asset: Asset | None = None,
    latest_price: Decimal | None = None,
    price_currency: str | None = None,
) -> HoldingOut:
    market_value = None
    unrealized_pnl = None
    if latest_price is not None and price_currency == h.cost_currency:
        market_value = h.quantity * latest_price
        if h.avg_cost is not None:
            unrealized_pnl = market_value - (h.quantity * h.avg_cost)

    return HoldingOut(
        id=h.id,
        account_id=h.account_id,
        account_name=h.account.name if h.account else None,
        asset_id=h.asset_id,
        symbol=asset.symbol if asset else None,
        asset_name=asset.name if asset else None,
        asset_class=asset.asset_class if asset else None,
        quantity=str(h.quantity),
        avg_cost=str(h.avg_cost) if h.avg_cost else None,
        cost_currency=h.cost_currency,
        current_price=str(latest_price) if latest_price else None,
        market_value=str(market_value) if market_value else None,
        unrealized_pnl=str(unrealized_pnl) if unrealized_pnl else None,
        last_synced_at=h.last_synced_at,
        created_at=h.created_at,
        updated_at=h.updated_at,
    )


@router.get("", response_model=ApiSuccess[list[HoldingOut]])
async def list_holdings(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    account_id: int | None = Query(None),
    asset_id: int | None = Query(None),
):
    from sqlalchemy.orm import selectinload
    stmt = (
        select(AssetHolding)
        .options(selectinload(AssetHolding.account), selectinload(AssetHolding.asset))
        .order_by(AssetHolding.id)
    )
    if account_id is not None:
        stmt = stmt.where(AssetHolding.account_id == account_id)
    if asset_id is not None:
        stmt = stmt.where(AssetHolding.asset_id == asset_id)
    result = await db.execute(stmt)
    holdings = result.scalars().all()

    out = []
    for h in holdings:
        latest_price = None
        price_currency = None

        if h.asset:
            price_stmt = (
                select(MarketPrice)
                .where(MarketPrice.asset_id == h.asset_id)
                .order_by(MarketPrice.quoted_at.desc())
                .limit(1)
            )
            price_result = await db.execute(price_stmt)
            latest_mp = price_result.scalar_one_or_none()
            if latest_mp:
                latest_price = latest_mp.price
                price_currency = latest_mp.currency

        out.append(_holding_to_out(h, h.asset, latest_price, price_currency))

    return ApiSuccess(data=out)


@router.get("/{holding_id}", response_model=ApiSuccess[HoldingOut])
async def get_holding(
    holding_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    stmt = (
        select(AssetHolding)
        .options(selectinload(AssetHolding.account), selectinload(AssetHolding.asset))
        .where(AssetHolding.id == holding_id)
    )
    result = await db.execute(stmt)
    holding = result.scalar_one_or_none()
    if not holding:
        raise NotFoundError("AssetHolding", holding_id)

    latest_price = None
    price_currency = None

    if holding.asset:
        price_stmt = (
            select(MarketPrice)
            .where(MarketPrice.asset_id == holding.asset_id)
            .order_by(MarketPrice.quoted_at.desc())
            .limit(1)
        )
        price_result = await db.execute(price_stmt)
        latest_mp = price_result.scalar_one_or_none()
        if latest_mp:
            latest_price = latest_mp.price
            price_currency = latest_mp.currency

    return ApiSuccess(data=_holding_to_out(holding, holding.asset, latest_price, price_currency))


@router.post("", response_model=ApiSuccess[HoldingOut], status_code=status.HTTP_201_CREATED)
async def create_holding(
    body: HoldingCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    holding = AssetHolding(
        account_id=body.account_id,
        asset_id=body.asset_id,
        quantity=Decimal(body.quantity),
        avg_cost=Decimal(body.avg_cost) if body.avg_cost else None,
        cost_currency=body.cost_currency,
        notes=body.notes,
    )
    db.add(holding)
    await db.flush()
    await db.refresh(holding, ["account", "asset"])
    return ApiSuccess(data=_holding_to_out(holding, holding.asset))


@router.patch("/{holding_id}", response_model=ApiSuccess[HoldingOut])
async def update_holding(
    holding_id: int,
    body: HoldingUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AssetHolding).where(AssetHolding.id == holding_id)
    result = await db.execute(stmt)
    holding = result.scalar_one_or_none()
    if not holding:
        raise NotFoundError("AssetHolding", holding_id)

    update_data = body.model_dump(exclude_unset=True)
    if "quantity" in update_data and update_data["quantity"] is not None:
        update_data["quantity"] = Decimal(update_data["quantity"])
    if "avg_cost" in update_data and update_data["avg_cost"] is not None:
        update_data["avg_cost"] = Decimal(update_data["avg_cost"])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_data["last_synced_at"] = now

    for key, value in update_data.items():
        setattr(holding, key, value)

    touch_updated_at(holding)
    await db.flush()
    await db.refresh(holding, ["account", "asset"])

    # Get latest price
    asset = holding.asset
    latest_price = None
    price_currency = None
    if asset:
        price_stmt = (
            select(MarketPrice)
            .where(MarketPrice.asset_id == holding.asset_id)
            .order_by(MarketPrice.quoted_at.desc())
            .limit(1)
        )
        price_result = await db.execute(price_stmt)
        latest_mp = price_result.scalar_one_or_none()
        if latest_mp:
            latest_price = latest_mp.price
            price_currency = latest_mp.currency

    return ApiSuccess(data=_holding_to_out(holding, asset, latest_price, price_currency))


@router.delete("/{holding_id}", response_model=ApiSuccess[dict])
async def delete_holding(
    holding_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AssetHolding).where(AssetHolding.id == holding_id)
    result = await db.execute(stmt)
    holding = result.scalar_one_or_none()
    if not holding:
        raise NotFoundError("AssetHolding", holding_id)

    await db.delete(holding)
    await db.flush()
    return ApiSuccess(data={"id": holding_id, "deleted": True})


@router.get("/portfolio/summary", response_model=ApiSuccess[PortfolioSummary])
async def portfolio_summary(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Calculate total portfolio value in base currency."""
    base_currency = settings.base_currency

    # Get all holdings with asset info and latest prices
    stmt = (
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
    )
    result = await db.execute(stmt)
    rows = result.all()

    by_class: dict[str, Decimal] = {}
    by_currency: dict[str, Decimal] = {}
    total = Decimal("0")

    for holding, asset in rows:
        # Get latest price
        price_stmt = (
            select(MarketPrice)
            .where(MarketPrice.asset_id == asset.id)
            .order_by(MarketPrice.quoted_at.desc())
            .limit(1)
        )
        price_result = await db.execute(price_stmt)
        latest = price_result.scalar_one_or_none()

        if latest is None:
            # No price data — skip or use 0
            continue

        # Value in price currency
        value = holding.quantity * latest.price

        # Convert to base currency if needed
        if latest.currency != base_currency:
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
                value = value * fx.rate
            else:
                # Can't convert — skip
                continue

        total += value
        by_class[asset.asset_class] = by_class.get(asset.asset_class, Decimal("0")) + value
        by_currency[latest.currency] = by_currency.get(latest.currency, Decimal("0")) + value

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ApiSuccess(data=PortfolioSummary(
        base_currency=base_currency,
        total_value=str(total),
        as_of=now,
        by_class={k: str(v) for k, v in by_class.items()},
        by_currency={k: str(v) for k, v in by_currency.items()},
    ))


@router.get("/portfolio/breakdown", response_model=ApiSuccess[PortfolioBreakdown])
async def portfolio_breakdown(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Return portfolio breakdown by class and currency (for pie charts)."""
    base_currency = settings.base_currency

    stmt = select(AssetHolding, Asset).join(Asset, AssetHolding.asset_id == Asset.id)
    result = await db.execute(stmt)
    rows = result.all()

    class_data: dict[str, dict] = {}
    currency_data: dict[str, dict] = {}

    for holding, asset in rows:
        price_stmt = (
            select(MarketPrice)
            .where(MarketPrice.asset_id == asset.id)
            .order_by(MarketPrice.quoted_at.desc())
            .limit(1)
        )
        price_result = await db.execute(price_stmt)
        latest = price_result.scalar_one_or_none()
        if not latest:
            continue

        value = holding.quantity * latest.price
        if latest.currency != base_currency:
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
                value = value * fx.rate
            else:
                continue

        # By class
        if asset.asset_class not in class_data:
            class_data[asset.asset_class] = {"value": Decimal("0"), "count": 0, "assets": []}
        class_data[asset.asset_class]["value"] += value
        class_data[asset.asset_class]["count"] += 1
        class_data[asset.asset_class]["assets"].append({
            "symbol": asset.symbol,
            "name": asset.name,
            "value": str(value),
        })

        # By currency
        if latest.currency not in currency_data:
            currency_data[latest.currency] = {"value": Decimal("0"), "count": 0}
        currency_data[latest.currency]["value"] += value
        currency_data[latest.currency]["count"] += 1

    # Convert Decimal values to str for JSON serialization
    for data in class_data.values():
        data["value"] = str(data["value"])
    for data in currency_data.values():
        data["value"] = str(data["value"])

    return ApiSuccess(data=PortfolioBreakdown(
        by_class=class_data,
        by_currency=currency_data,
    ))
