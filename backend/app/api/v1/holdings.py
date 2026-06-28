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
from app.models import Account, AssetHolding, Asset, MarketPrice, FxRate
from app.models import touch_updated_at
from app.schemas import (
    ApiSuccess,
    CompositionEntry,
    HoldingCreate,
    HoldingOut,
    HoldingUpdate,
    NetWorthOut,
    PortfolioBreakdown,
    PortfolioComposition,
    PortfolioSummary,
    PortfolioValuePoint,
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
    # market_value is computed whenever we have a price, regardless of cost_currency.
    # Wallet/CEX-synced crypto holdings often have cost_currency=None (unknown cost
    # basis), but we still know the current market value from the latest price.
    market_value = None
    market_value_currency = None
    unrealized_pnl = None
    if latest_price is not None:
        market_value = h.quantity * latest_price
        market_value_currency = price_currency
        # unrealized_pnl requires cost_currency to match price_currency so the
        # subtraction is in the same unit. Skip when cost is unknown or mismatched.
        if h.avg_cost is not None and price_currency == h.cost_currency:
            unrealized_pnl = market_value - (h.quantity * h.avg_cost)

    return HoldingOut(
        id=h.id,
        account_id=h.account_id,
        account_name=h.account.name if h.account else None,
        asset_id=h.asset_id,
        symbol=asset.symbol if asset else None,
        asset_name=asset.name if asset else None,
        asset_class=asset.asset_class if asset else None,
        chain=h.chain,
        quantity=str(h.quantity),
        avg_cost=str(h.avg_cost) if h.avg_cost else None,
        cost_currency=h.cost_currency,
        current_price=str(latest_price) if latest_price else None,
        price_currency=price_currency,
        market_value=str(market_value) if market_value is not None else None,
        market_value_currency=market_value_currency,
        unrealized_pnl=str(unrealized_pnl) if unrealized_pnl is not None else None,
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

    # Get all holdings with asset info and latest prices.
    # Only include holdings from accounts that are opted-in to grand-total
    # aggregation and are not soft-deleted; skip inactive holdings too.
    stmt = (
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
        .join(Account, Account.id == AssetHolding.account_id)
        .where(
            Account.include_in_total == True,  # noqa: E712
            Account.deleted_at.is_(None),
            AssetHolding.is_active == True,  # noqa: E712
        )
    )
    result = await db.execute(stmt)
    rows = result.all()

    by_class: dict[str, Decimal] = {}
    # Sprint 4 FIX-22 (review V3 §V3-P1-5): the previous shape was
    # `by_currency[quote_currency] = base_value`, which mislabelled values:
    # callers reading `by_currency["EUR"]` got CNY-equivalent numbers.
    # New shape: `{quote_currency: {original_value, base_value}}`. Total
    # only counts rows with a successful FX path.
    by_currency: dict[str, dict[str, Decimal]] = {}
    fx_missing: list[dict[str, str]] = []
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
            continue

        original_value = holding.quantity * latest.price
        bucket = by_currency.setdefault(
            latest.currency, {"original_value": Decimal("0"), "base_value": Decimal("0")}
        )
        bucket["original_value"] += original_value

        if latest.currency == base_currency:
            converted = original_value
        else:
            converted = await _convert_to_base(db, original_value, latest.currency, base_currency)
        if converted is None:
            fx_missing.append({
                "asset_id": str(asset.id),
                "symbol": asset.symbol,
                "quote_currency": latest.currency,
                "original_value": str(original_value),
            })
            continue

        bucket["base_value"] += converted
        total += converted
        by_class[asset.asset_class] = by_class.get(asset.asset_class, Decimal("0")) + converted

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ApiSuccess(data=PortfolioSummary(
        base_currency=base_currency,
        total_value=str(total),
        as_of=now,
        by_class={k: str(v) for k, v in by_class.items()},
        by_currency={
            k: {"original_value": str(v["original_value"]), "base_value": str(v["base_value"])}
            for k, v in by_currency.items()
        },
        fx_missing=fx_missing,
    ))


@router.get("/portfolio/breakdown", response_model=ApiSuccess[PortfolioBreakdown])
async def portfolio_breakdown(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Return portfolio breakdown by class and currency (for pie charts)."""
    base_currency = settings.base_currency

    stmt = (
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
        .join(Account, Account.id == AssetHolding.account_id)
        .where(
            Account.include_in_total == True,  # noqa: E712
            Account.deleted_at.is_(None),
            AssetHolding.is_active == True,  # noqa: E712
        )
    )
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

        # Sprint 4 FIX-22: track original_value (in quote currency) and
        # base_value separately so callers don't conflate the two.
        original_value = holding.quantity * latest.price
        if latest.currency == base_currency:
            converted = original_value
        else:
            converted = await _convert_to_base(db, original_value, latest.currency, base_currency)
        if converted is None:
            continue
        value = converted

        # By class (sums in base currency)
        if asset.asset_class not in class_data:
            class_data[asset.asset_class] = {"value": Decimal("0"), "count": 0, "assets": []}
        class_data[asset.asset_class]["value"] += value
        class_data[asset.asset_class]["count"] += 1
        class_data[asset.asset_class]["assets"].append({
            "symbol": asset.symbol,
            "name": asset.name,
            "value": str(value),
            "currency": latest.currency,
        })

        # By currency (key = quote currency; values split into original + base)
        if latest.currency not in currency_data:
            currency_data[latest.currency] = {
                "original_value": Decimal("0"),
                "base_value": Decimal("0"),
                "count": 0,
            }
        currency_data[latest.currency]["original_value"] += original_value
        currency_data[latest.currency]["base_value"] += value
        currency_data[latest.currency]["count"] += 1

    # Convert Decimal values to str for JSON serialization
    for data in class_data.values():
        data["value"] = str(data["value"])
    for data in currency_data.values():
        data["original_value"] = str(data["original_value"])
        data["base_value"] = str(data["base_value"])

    return ApiSuccess(data=PortfolioBreakdown(
        by_class=class_data,
        by_currency=currency_data,
    ))


# FX conversion now lives in services/valuation/fx.py so the holdings
# endpoints and the account-balance aggregation share one implementation.
# These thin wrappers preserve the original names/signatures used by tests
# (test_usdt_alias.py) and the call sites above.
async def _latest_fx_rate(
    db: AsyncSession, base: str, quote: str
) -> Decimal | None:
    from app.services.valuation.fx import latest_fx_rate

    return await latest_fx_rate(db, base, quote)


async def _convert_to_base(
    db: AsyncSession,
    amount: Decimal,
    src_currency: str,
    base_currency: str,
) -> Decimal | None:
    from app.services.valuation.fx import convert_to_base

    return await convert_to_base(db, amount, src_currency, base_currency)


@router.get("/portfolio/net-worth", response_model=ApiSuccess[NetWorthOut])
async def net_worth(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Aggregate cash (account balances) + investments (holdings market value) into total net worth.

    Honours per-account ``include_in_total`` — accounts the user
    explicitly excluded are dropped from BOTH cash and investment
    aggregation but still appear in the per-account balance list.

    Math lives in ``services/valuation/net_worth.compute_net_worth`` so the
    monthly portfolio snapshot job produces identical numbers.
    """
    from app.services.valuation.net_worth import compute_net_worth

    r = await compute_net_worth(db, settings.base_currency)
    return ApiSuccess(data=NetWorthOut(
        base_currency=r.base_currency,
        cash_total=str(r.cash_total),
        investment_total=str(r.investment_total),
        net_worth=str(r.net_worth),
        cash_by_currency=r.cash_by_currency,
        investment_by_currency={
            k: {"original_value": str(v["original_value"]), "base_value": str(v["base_value"])}
            for k, v in r.investment_by_currency.items()
        },
        as_of=r.as_of,
    ))


@router.get("/portfolio/composition", response_model=ApiSuccess[PortfolioComposition])
async def portfolio_composition(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Net-worth composition (cash + investments) by logical asset.

    Third distribution view: stablecoins merged into one bucket, the same coin
    summed across accounts/exchanges, dust (< €0.1) dropped, and small long-tail
    positions (< €20) folded into per-category 小额 buckets. Folded to base.
    """
    from app.services.valuation.composition import compute_composition

    r = await compute_composition(db, settings.base_currency)
    return ApiSuccess(data=PortfolioComposition(
        base_currency=r.base_currency,
        total=str(r.total),
        entries=[CompositionEntry(**e) for e in r.entries],
        dust_excluded_count=r.dust_excluded_count,
    ))


@router.get("/portfolio/value-history", response_model=ApiSuccess[list[PortfolioValuePoint]])
async def portfolio_value_history(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Monthly portfolio-value snapshots, oldest first.

    Forward-captured by the scheduler (see services/valuation/snapshot.py) —
    history cannot be reconstructed, so the series begins when snapshotting
    started and grows one point per month.
    """
    from app.models import PortfolioSnapshot

    rows = (
        await db.execute(select(PortfolioSnapshot).order_by(PortfolioSnapshot.period))
    ).scalars().all()
    return ApiSuccess(data=[
        PortfolioValuePoint(
            period=s.period,
            base_currency=s.base_currency,
            cash_total=str(s.cash_total),
            investment_total=str(s.investment_total),
            net_worth=str(s.net_worth),
            captured_at=s.captured_at,
        )
        for s in rows
    ])
