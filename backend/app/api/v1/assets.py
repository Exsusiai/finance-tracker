"""Asset definition routes — CRUD."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.errors import NotFoundError
from app.db import get_db
from app.models import Asset, MarketPrice
from app.schemas import ApiSuccess, AssetCreate, AssetOut, AssetUpdate

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


def _asset_to_out(a: Asset, latest_price: MarketPrice | None = None) -> AssetOut:
    return AssetOut(
        id=a.id,
        symbol=a.symbol,
        name=a.name,
        asset_class=a.asset_class,
        currency=a.currency,
        market=a.market,
        data_source=a.data_source,
        data_source_id=a.data_source_id,
        decimals=a.decimals,
        notes=a.notes,
        created_at=a.created_at,
        updated_at=a.updated_at,
        latest_price=str(latest_price.price) if latest_price else None,
        latest_price_currency=latest_price.currency if latest_price else None,
    )


@router.get("", response_model=ApiSuccess[list[AssetOut]])
async def list_assets(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    asset_class: str | None = Query(None),
):
    stmt = select(Asset).order_by(Asset.asset_class, Asset.symbol)
    if asset_class:
        stmt = stmt.where(Asset.asset_class == asset_class)
    result = await db.execute(stmt)
    assets = result.scalars().all()
    return ApiSuccess(data=[_asset_to_out(a) for a in assets])


@router.post("", response_model=ApiSuccess[AssetOut], status_code=status.HTTP_201_CREATED)
async def create_asset(
    body: AssetCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    asset = Asset(
        symbol=body.symbol,
        name=body.name,
        asset_class=body.asset_class,
        currency=body.currency,
        market=body.market,
        data_source=body.data_source,
        data_source_id=body.data_source_id,
        decimals=body.decimals,
        notes=body.notes,
    )
    db.add(asset)
    await db.flush()
    return ApiSuccess(data=_asset_to_out(asset))


@router.get("/{asset_id}", response_model=ApiSuccess[AssetOut])
async def get_asset(
    asset_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Asset).where(Asset.id == asset_id)
    result = await db.execute(stmt)
    asset = result.scalar_one_or_none()
    if not asset:
        raise NotFoundError("Asset", asset_id)

    # Get latest price
    price_stmt = (
        select(MarketPrice)
        .where(MarketPrice.asset_id == asset_id)
        .order_by(MarketPrice.quoted_at.desc())
        .limit(1)
    )
    price_result = await db.execute(price_stmt)
    latest_price = price_result.scalar_one_or_none()

    return ApiSuccess(data=_asset_to_out(asset, latest_price))


@router.patch("/{asset_id}", response_model=ApiSuccess[AssetOut])
async def update_asset(
    asset_id: int,
    body: AssetUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Asset).where(Asset.id == asset_id)
    result = await db.execute(stmt)
    asset = result.scalar_one_or_none()
    if not asset:
        raise NotFoundError("Asset", asset_id)

    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(asset, key, value)

    await db.flush()
    return ApiSuccess(data=_asset_to_out(asset))
