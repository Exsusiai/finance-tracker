"""Market data engine — fetches prices from various sources."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, MarketPrice, FxRate
from app.services.wallet_sync.orchestrator import refresh_onchain_native_prices

logger = logging.getLogger(__name__)

# Stock (a_share/eu_stock/us_stock) sources whose data_source_id is NOT a
# Yahoo Finance ticker — IBKR carries a Flex conid, Trade Republic an ISIN
# (see broker_sync/upsert.py). Feeding either to yfinance.Ticker() is at
# best a no-op (empty history) and at worst mis-resolves to an unrelated
# instrument that happens to share the string. Those sources already get
# live prices from their own statement/websocket feed during account sync
# (F22, 2026-09).
_NON_YAHOO_STOCK_SOURCES = ("ibkr", "traderepublic")


async def refresh_crypto_prices(db: AsyncSession) -> dict[str, Any]:
    """Refresh crypto prices for every crypto asset.

    Two paths, merged:
    - Legacy/manual crypto assets (CoinGecko coin-id in ``data_source_id``)
      via ``/simple/price?ids=``, below.
    - Wallet/exchange-synced holdings (onchain contract tokens, native
      chain coins, CEX ticker symbols) via
      ``wallet_sync.orchestrator.refresh_onchain_native_prices()`` — the
      SAME batched CoinGecko helper the per-account post-sync refresh
      uses. F22 (2026-09): this function used to hard-exclude those rows
      entirely (comment below), so neither the scheduler nor the
      "refresh market data" button ever re-priced them between account
      syncs — only a sync itself did. Reusing the existing per-account
      function (with ``account_id=None``) closes that gap without a
      second implementation.
    """
    result = {"prices_updated": 0, "errors": []}
    try:
        # Excludes ``data_source IN ('onchain','native')`` — those rows are
        # created by the P1-4 wallet_sync pipeline and have a contract address
        # (or no source id at all) as ``data_source_id``. Sending those values
        # to ``/simple/price?ids=`` returns 404 because the endpoint expects a
        # CoinGecko coin id. They're covered by refresh_onchain_native_prices()
        # below instead. (V5-P2-4, 2026-05-20)
        stmt = select(Asset).where(
            Asset.asset_class == "crypto",
            Asset.data_source.is_not(None),
            Asset.data_source.notin_(("onchain", "native")),
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

    try:
        result["prices_updated"] += await refresh_onchain_native_prices(db)
    except Exception as e:
        result["errors"].append(f"wallet_synced_crypto: {e}")

    await db.flush()
    return result


async def refresh_stock_prices(db: AsyncSession) -> dict[str, Any]:
    """Refresh stock prices (yfinance) for a-share / eu_stock / us_stock assets.

    F22: skips ``data_source IN ('ibkr', 'traderepublic')`` — see
    ``_NON_YAHOO_STOCK_SOURCES`` above. A blacklist (rather than requiring
    ``data_source == 'yfinance'``) keeps manually-created assets (which may
    leave ``data_source`` unset) working exactly as before.
    """
    result = {"prices_updated": 0, "errors": [], "skipped": 0}
    try:
        # NOT IN excludes NULL rows under SQL's three-valued logic — a
        # manually-created asset with data_source=None would otherwise
        # silently drop out of both queries below. Spell out "NULL or not
        # a broker source" instead.
        is_yahoo_source = or_(
            Asset.data_source.is_(None),
            Asset.data_source.notin_(_NON_YAHOO_STOCK_SOURCES),
        )
        stmt = select(Asset).where(
            Asset.asset_class.in_(["a_share", "eu_stock", "us_stock"]),
            Asset.data_source_id.is_not(None),
            is_yahoo_source,
        )
        assets = (await db.execute(stmt)).scalars().all()
        skipped_stmt = select(Asset.id).where(
            Asset.asset_class.in_(["a_share", "eu_stock", "us_stock"]),
            Asset.data_source_id.is_not(None),
            Asset.data_source.in_(_NON_YAHOO_STOCK_SOURCES),
        )
        result["skipped"] = len((await db.execute(skipped_stmt)).all())
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
