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


async def refresh_all_market_data(db: AsyncSession) -> dict[str, Any]:
    """Refresh market data for all assets that have a data_source configured.
    
    Returns summary of what was refreshed.
    """
    result = {
        "prices_updated": 0,
        "fx_updated": 0,
        "errors": [],
    }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Refresh crypto prices (CoinGecko)
    try:
        crypto_stmt = select(Asset).where(
            Asset.asset_class == "crypto",
            Asset.data_source.is_not(None),
        )
        crypto_result = await db.execute(crypto_stmt)
        crypto_assets = crypto_result.scalars().all()

        for asset in crypto_assets:
            try:
                price_data = await _fetch_crypto_price(asset)
                if price_data:
                    mp = MarketPrice(
                        asset_id=asset.id,
                        quoted_at=price_data["quoted_at"],
                        price=price_data["price"],
                        currency=price_data["currency"],
                        source=price_data["source"],
                    )
                    db.add(mp)
                    result["prices_updated"] += 1
            except Exception as e:
                result["errors"].append(f"{asset.symbol}: {e}")
    except Exception as e:
        result["errors"].append(f"crypto: {e}")

    # Refresh stock prices (yfinance)
    try:
        stock_stmt = select(Asset).where(
            Asset.asset_class.in_(["a_share", "eu_stock", "us_stock"]),
            Asset.data_source_id.is_not(None),
        )
        stock_result = await db.execute(stock_stmt)
        stock_assets = stock_result.scalars().all()

        for asset in stock_assets:
            try:
                price_data = await _fetch_stock_price(asset)
                if price_data:
                    mp = MarketPrice(
                        asset_id=asset.id,
                        quoted_at=price_data["quoted_at"],
                        price=price_data["price"],
                        currency=price_data["currency"],
                        source=price_data["source"],
                    )
                    db.add(mp)
                    result["prices_updated"] += 1
            except Exception as e:
                result["errors"].append(f"{asset.symbol}: {e}")
    except Exception as e:
        result["errors"].append(f"stocks: {e}")

    # Refresh FX rates
    try:
        fx_data = await _fetch_fx_rates()
        for fx in fx_data:
            rate = FxRate(
                base_currency=fx["base"],
                quote_currency=fx["quote"],
                quoted_at=fx["quoted_at"],
                rate=fx["rate"],
                source=fx["source"],
            )
            db.add(rate)
            result["fx_updated"] += 1
    except Exception as e:
        result["errors"].append(f"fx: {e}")

    await db.flush()
    return result


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
    """Fetch FX rates from exchangerate.host."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.exchangerate.host/latest",
                params={"base": "CNY", "symbols": "EUR,USD,GBP,JPY"},
            )
            resp.raise_for_status()
            data = resp.json()

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rates = []
            if "rates" in data:
                for currency, rate in data["rates"].items():
                    rates.append({
                        "base": "CNY",
                        "quote": currency,
                        "quoted_at": now,
                        "rate": Decimal(str(rate)),
                        "source": "exchangerate.host",
                    })
            return rates
    except Exception as e:
        logger.warning(f"Failed to fetch FX rates: {e}")
        return []
