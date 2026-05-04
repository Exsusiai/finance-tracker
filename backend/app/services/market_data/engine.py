"""Market data engine — fetches prices from various sources."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, MarketPrice, FxRate

logger = logging.getLogger(__name__)


async def refresh_crypto_prices(db: AsyncSession) -> dict[str, Any]:
    """Refresh crypto prices (CoinGecko) for all crypto assets with data_source set."""
    result = {"prices_updated": 0, "errors": []}
    try:
        stmt = select(Asset).where(
            Asset.asset_class == "crypto",
            Asset.data_source.is_not(None),
        )
        assets = (await db.execute(stmt)).scalars().all()
        for asset in assets:
            try:
                data = await _fetch_crypto_price(asset)
                if data:
                    db.add(MarketPrice(
                        asset_id=asset.id,
                        quoted_at=data["quoted_at"],
                        price=data["price"],
                        currency=data["currency"],
                        source=data["source"],
                    ))
                    result["prices_updated"] += 1
            except Exception as e:
                result["errors"].append(f"{asset.symbol}: {e}")
    except Exception as e:
        result["errors"].append(f"crypto: {e}")
    await db.flush()
    return result


async def refresh_stock_prices(db: AsyncSession) -> dict[str, Any]:
    """Refresh stock prices (yfinance) for a-share / eu_stock / us_stock assets."""
    result = {"prices_updated": 0, "errors": []}
    try:
        stmt = select(Asset).where(
            Asset.asset_class.in_(["a_share", "eu_stock", "us_stock"]),
            Asset.data_source_id.is_not(None),
        )
        assets = (await db.execute(stmt)).scalars().all()
        for asset in assets:
            try:
                data = await _fetch_stock_price(asset)
                if data:
                    db.add(MarketPrice(
                        asset_id=asset.id,
                        quoted_at=data["quoted_at"],
                        price=data["price"],
                        currency=data["currency"],
                        source=data["source"],
                    ))
                    result["prices_updated"] += 1
            except Exception as e:
                result["errors"].append(f"{asset.symbol}: {e}")
    except Exception as e:
        result["errors"].append(f"stocks: {e}")
    await db.flush()
    return result


async def refresh_fx(db: AsyncSession) -> dict[str, Any]:
    """Refresh FX rates from open.er-api.com (with frankfurter fallback)."""
    result = {"fx_updated": 0, "errors": []}
    try:
        for fx in await _fetch_fx_rates():
            db.add(FxRate(
                base_currency=fx["base"],
                quote_currency=fx["quote"],
                quoted_at=fx["quoted_at"],
                rate=fx["rate"],
                source=fx["source"],
            ))
            result["fx_updated"] += 1
    except Exception as e:
        result["errors"].append(f"fx: {e}")
    await db.flush()
    return result


async def refresh_all_market_data(db: AsyncSession) -> dict[str, Any]:
    """Run all refreshers in sequence and merge their results."""
    crypto = await refresh_crypto_prices(db)
    stocks = await refresh_stock_prices(db)
    fx = await refresh_fx(db)
    return {
        "prices_updated": crypto["prices_updated"] + stocks["prices_updated"],
        "fx_updated": fx["fx_updated"],
        "errors": crypto["errors"] + stocks["errors"] + fx["errors"],
    }


async def _fetch_crypto_price(asset: Asset) -> dict | None:
    """Fetch crypto price from CoinGecko."""
    try:
        import httpx

        if not asset.data_source_id:
            return None

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": asset.data_source_id,
                    "vs_currencies": asset.currency.lower(),
                },
            )
            resp.raise_for_status()
            data = resp.json()

            coin_id = asset.data_source_id
            if coin_id in data:
                price_str = str(data[coin_id].get(asset.currency.lower(), 0))
                return {
                    "quoted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "price": Decimal(price_str),
                    "currency": asset.currency,
                    "source": "coingecko",
                }
    except Exception as e:
        logger.warning(f"Failed to fetch crypto price for {asset.symbol}: {e}")
    return None


async def _fetch_stock_price(asset: Asset) -> dict | None:
    """Fetch stock price via yfinance (run in thread pool since it's sync)."""
    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        if not asset.data_source_id:
            return None

        def _sync_fetch():
            import yfinance as yf
            ticker = yf.Ticker(asset.data_source_id)
            hist = ticker.history(period="1d")
            if hist.empty:
                return None
            return {
                "price": Decimal(str(hist["Close"].iloc[-1])),
                "quoted_at": hist.index[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            data = await loop.run_in_executor(pool, _sync_fetch)

        if data:
            return {
                **data,
                "currency": asset.currency,
                "source": "yfinance",
            }
    except Exception as e:
        logger.warning(f"Failed to fetch stock price for {asset.symbol}: {e}")
    return None


async def _fetch_fx_rates() -> list[dict]:
    """Fetch FX rates from a free provider.

    open.er-api.com (free, no key) returns rates with USD as the implicit base,
    so we query base=CNY and re-emit each (CNY → quote) directly.
    """
    import httpx

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rates: list[dict] = []

    # Primary: open.er-api.com (free, no key)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/CNY")
            resp.raise_for_status()
            data = resp.json()

        if data.get("result") == "success" and isinstance(data.get("rates"), dict):
            for currency, rate in data["rates"].items():
                if currency == "CNY":
                    continue
                try:
                    rates.append({
                        "base": "CNY",
                        "quote": currency,
                        "quoted_at": now,
                        "rate": Decimal(str(rate)),
                        "source": "open.er-api.com",
                    })
                except Exception:
                    continue
            if rates:
                return rates
    except Exception as e:
        logger.warning(f"open.er-api FX fetch failed: {e}")

    # Fallback: frankfurter.app (free, ECB data, limited list)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.frankfurter.app/latest",
                params={"from": "CNY", "to": "EUR,USD,GBP,JPY,HKD,CHF"},
            )
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data.get("rates"), dict):
            for currency, rate in data["rates"].items():
                rates.append({
                    "base": "CNY",
                    "quote": currency,
                    "quoted_at": now,
                    "rate": Decimal(str(rate)),
                    "source": "frankfurter.app",
                })
    except Exception as e:
        logger.warning(f"frankfurter FX fetch failed: {e}")

    return rates
