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
    HoldingCreate,
    HoldingOut,
    HoldingUpdate,
    NetWorthOut,
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


async def _latest_fx_rate(
    db: AsyncSession, base: str, quote: str
) -> Decimal | None:
    """Return the newest FxRate.rate for (base → quote), or None."""
    stmt = (
        select(FxRate)
        .where(FxRate.base_currency == base, FxRate.quote_currency == quote)
        .order_by(FxRate.quoted_at.desc())
        .limit(1)
    )
    fx = (await db.execute(stmt)).scalar_one_or_none()
    return fx.rate if fx else None


async def _convert_to_base(
    db: AsyncSession,
    amount: Decimal,
    src_currency: str,
    base_currency: str,
) -> Decimal | None:
    """Convert amount from src_currency → base_currency.

    Strategy:
      1. Same currency → return amount
      2. Direct rate (src → base) → amount * rate
      3. Inverse rate (base → src) → amount / rate
      4. Triangulate via USD pivot
      Returns None when no path is available.
    """
    # USDT is a USD-pegged stablecoin; the fiat FX scheduler doesn't
    # populate USDT rows, so without this alias every crypto holding
    # (priced in USDT by the wallet_sync pipeline) would be silently
    # dropped from net_worth aggregation. Same trick for USDC / DAI —
    # all major USD-pegged stablecoins.
    _USD_PEGGED = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX"}
    if src_currency in _USD_PEGGED:
        src_currency = "USD"
    if base_currency in _USD_PEGGED:
        base_currency = "USD"

    if src_currency == base_currency:
        return amount

    direct = await _latest_fx_rate(db, src_currency, base_currency)
    if direct is not None and direct > 0:
        return amount * direct

    inverse = await _latest_fx_rate(db, base_currency, src_currency)
    if inverse is not None and inverse > 0:
        return amount / inverse

    # Triangulate via a pivot currency. CNY is critical because the live
    # FX scheduler emits everything keyed `base_currency='CNY'`, so for
    # most (src, base) combos the only viable bridge is CNY. USD / EUR
    # kept for legacy / cached-rate paths.
    for pivot in ("CNY", "USD", "EUR"):
        if pivot in (src_currency, base_currency):
            continue
        a_direct = await _latest_fx_rate(db, src_currency, pivot)
        a_inverse = (
            await _latest_fx_rate(db, pivot, src_currency)
            if a_direct is None
            else None
        )
        a = a_direct if a_direct is not None else (
            (Decimal(1) / a_inverse) if (a_inverse is not None and a_inverse > 0) else None
        )
        b_direct = await _latest_fx_rate(db, pivot, base_currency)
        b_inverse = (
            await _latest_fx_rate(db, base_currency, pivot)
            if b_direct is None
            else None
        )
        b = b_direct if b_direct is not None else (
            (Decimal(1) / b_inverse) if (b_inverse is not None and b_inverse > 0) else None
        )
        if a is not None and b is not None:
            return amount * a * b

    return None


@router.get("/portfolio/net-worth", response_model=ApiSuccess[NetWorthOut])
async def net_worth(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Aggregate cash (account balances) + investments (holdings market value) into total net worth.

    Honours per-account ``include_in_total`` — accounts the user
    explicitly excluded are dropped from BOTH cash and investment
    aggregation but still appear in the per-account balance list.
    """
    base_currency = settings.base_currency

    # 1. Cash: account balances grouped by currency. JOIN accounts to
    # drop opted-out rows from the SUM.
    balances_stmt = text("""
        SELECT v.currency, SUM(v.balance) AS total
        FROM v_account_balance v
        JOIN accounts a ON a.id = v.account_id
        WHERE a.include_in_total = 1 AND a.deleted_at IS NULL
        GROUP BY v.currency
    """)
    balances_result = await db.execute(balances_stmt)
    cash_total = Decimal("0")
    cash_details: dict[str, dict[str, str]] = {}
    for currency, total in balances_result.all():
        original = total if isinstance(total, Decimal) else Decimal(str(total or 0))
        converted = await _convert_to_base(db, original, currency, base_currency)
        if converted is not None:
            cash_total += converted
            cash_details[currency] = {
                "original": str(original),
                "converted": str(converted),
            }
        else:
            # No FX rate — record original but cannot include in total
            cash_details[currency] = {
                "original": str(original),
                "converted": "",
            }

    # 2. Investments: reuse portfolio_summary logic (skip rows missing price/FX)
    # Sprint 4 FIX-22 (review V3 §V3-P1-5): track original_value AND
    # base_value per quote currency, so callers reading
    # investment_by_currency["EUR"] aren't surprised that the value is in CNY.
    # P2.3: same opt-out check as the cash side — JOIN accounts +
    # filter include_in_total + is_active (skip soft-deleted holdings
    # too, which the per-chain re-sync may have left at quantity=0).
    inv_stmt = (
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
        .join(Account, Account.id == AssetHolding.account_id)
        .where(
            Account.include_in_total == True,  # noqa: E712
            Account.deleted_at.is_(None),
            AssetHolding.is_active == True,  # noqa: E712
        )
    )
    inv_result = await db.execute(inv_stmt)
    investment_total = Decimal("0")
    investment_by_currency: dict[str, dict[str, Decimal]] = {}
    for holding, asset in inv_result.all():
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
        bucket = investment_by_currency.setdefault(
            latest.currency,
            {"original_value": Decimal("0"), "base_value": Decimal("0")},
        )
        bucket["original_value"] += original_value
        if latest.currency == base_currency:
            converted = original_value
        else:
            converted = await _convert_to_base(db, original_value, latest.currency, base_currency)
        if converted is None:
            continue
        bucket["base_value"] += converted
        investment_total += converted

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ApiSuccess(data=NetWorthOut(
        base_currency=base_currency,
        cash_total=str(cash_total),
        investment_total=str(investment_total),
        net_worth=str(cash_total + investment_total),
        cash_by_currency=cash_details,
        investment_by_currency={
            k: {"original_value": str(v["original_value"]), "base_value": str(v["base_value"])}
            for k, v in investment_by_currency.items()
        },
        as_of=now,
    ))
