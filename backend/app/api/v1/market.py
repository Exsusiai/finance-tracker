"""Market data routes — prices, FX rates, refresh triggers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.config import get_settings
from app.core.errors import NotFoundError, MarketDataError
from app.db import get_db
from app.models import MarketPrice, Asset, FxRate
from app.schemas import (
    ApiSuccess,
    FxRateOut,
    MarketPriceOut,
    MarketRefreshStatus,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]
settings = get_settings()

# In-memory state for refresh tracking
_refresh_state = {
    "last_refreshed_at": None,
    "status": "idle",
    "error_message": None,
}


@router.get("/prices/{asset_id}", response_model=ApiSuccess[list[MarketPriceOut]])
async def get_asset_prices(
    asset_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    range: str = Query("1m", pattern=r"^(1d|1w|1m|3m|6m|1y|all)$"),
    limit: int = Query(100, ge=1, le=1000),
):
    """Get price history for an asset."""
    # Verify asset exists
    asset_stmt = select(Asset).where(Asset.id == asset_id)
    asset_result = await db.execute(asset_stmt)
    asset = asset_result.scalar_one_or_none()
    if not asset:
        raise NotFoundError("Asset", asset_id)

    # Calculate time filter based on range
    now = datetime.now(timezone.utc)
    time_map = {
        "1d": {"days": 1},
        "1w": {"days": 7},
        "1m": {"days": 30},
        "3m": {"days": 90},
        "6m": {"days": 180},
        "1y": {"days": 365},
    }

    stmt = (
        select(MarketPrice)
        .where(MarketPrice.asset_id == asset_id)
        .order_by(MarketPrice.quoted_at.desc())
        .limit(limit)
    )

    if range != "all":
        delta_kwargs = time_map.get(range, {"days": 30})
        from datetime import timedelta
        cutoff = (now - timedelta(**delta_kwargs)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stmt = stmt.where(MarketPrice.quoted_at >= cutoff)

    result = await db.execute(stmt)
    prices = result.scalars().all()

    return ApiSuccess(data=[
        MarketPriceOut(
            asset_id=p.asset_id,
            symbol=asset.symbol,
            quoted_at=p.quoted_at,
            price=str(p.price),
            currency=p.currency,
            source=p.source,
        )
        for p in prices
    ])


@router.post("/refresh", response_model=ApiSuccess[dict])
async def trigger_market_refresh(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Trigger an immediate market data refresh (async)."""
    _refresh_state["status"] = "running"
    _refresh_state["error_message"] = None

    try:
        from app.services.market_data.engine import refresh_all_market_data

        result = await refresh_all_market_data(db)
        _refresh_state["last_refreshed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _refresh_state["status"] = "idle"
        return ApiSuccess(data=result)
    except Exception as e:
        _refresh_state["status"] = "error"
        _refresh_state["error_message"] = str(e)
        raise MarketDataError(str(e))


@router.get("/refresh/status", response_model=ApiSuccess[MarketRefreshStatus])
async def get_refresh_status(_token: _auth):
    """Get the status of the last market data refresh."""
    return ApiSuccess(data=MarketRefreshStatus(
        last_refreshed_at=_refresh_state.get("last_refreshed_at"),
        status=_refresh_state.get("status", "idle"),
        error_message=_refresh_state.get("error_message"),
    ))


@router.get("/fx", response_model=ApiSuccess[list[FxRateOut]])
async def get_fx_rates(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    base: str = Query("CNY"),
    quote: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
):
    """Get FX rate snapshots."""
    stmt = select(FxRate).where(FxRate.base_currency == base)
    if quote:
        stmt = stmt.where(FxRate.quote_currency == quote)
    stmt = stmt.order_by(FxRate.quoted_at.desc()).limit(limit)
    result = await db.execute(stmt)
    rates = result.scalars().all()

    return ApiSuccess(data=[
        FxRateOut(
            base_currency=r.base_currency,
            quote_currency=r.quote_currency,
            quoted_at=r.quoted_at,
            rate=str(r.rate),
            source=r.source,
        )
        for r in rates
    ])
