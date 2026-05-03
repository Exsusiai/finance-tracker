"""Asset search engine — discovers symbols / data_source_ids for crypto + stocks.

Uses external APIs:
- CoinGecko `/search` for crypto
- Yahoo Finance `/v1/finance/search` for stocks (no auth required)

Results are cached in-memory for 5 minutes per (source, query) key to mitigate
CoinGecko free-tier rate limits (10-50 req/min).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL_SEC = 300
_MAX_RESULTS = 10
_HTTP_TIMEOUT = 10.0

_COINGECKO_SEARCH_URL = "https://api.coingecko.com/api/v3/search"


def _cache_get(source: str, query: str) -> list[dict[str, Any]] | None:
    key = (source, query.lower())
    entry = _CACHE.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.time() >= expires_at:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(source: str, query: str, value: list[dict[str, Any]]) -> None:
    _CACHE[(source, query.lower())] = (time.time() + _CACHE_TTL_SEC, value)


def _classify_stock(exchange: str | None, currency: str | None) -> tuple[str, str | None]:
    """Map yahoo `exchange` + `currency` to (asset_class, market_label)."""
    ex = (exchange or "").upper()
    cur = (currency or "").upper()
    if ex in {"SHH", "SHZ", "SHA", "SSE", "SZSE"} or cur == "CNY":
        return "a_share", "CN"
    if ex in {"HKG"} or cur == "HKD":
        return "us_stock", "HK"
    if ex in {"NMS", "NYQ", "NAS", "NASDAQ", "NYSE", "ASE", "AMEX", "BATS", "PCX"} or cur == "USD":
        return "us_stock", "US"
    eu_exchanges = {
        "GER", "FRA", "STU", "MUN", "DUS", "HAM", "BER",
        "PAR", "AMS", "BRU", "LIS", "MIL", "MTA", "MCE",
        "LSE", "LON", "VIE", "OSL", "STO", "HEL", "CPH",
        "SWX", "EBS",
    }
    eu_currencies = {"EUR", "GBP", "GBP.", "CHF", "SEK", "NOK", "DKK"}
    if ex in eu_exchanges or cur in eu_currencies:
        return "eu_stock", "EU"
    return "us_stock", ex or None


async def search_crypto(query: str) -> list[dict[str, Any]]:
    """Search CoinGecko for crypto matches.

    Each result dict: {symbol, name, asset_class, currency, data_source,
    data_source_id, thumb}. Max 10 results.
    """
    cached = _cache_get("crypto", query)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_COINGECKO_SEARCH_URL, params={"query": query})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("CoinGecko search failed for %r: %s", query, e)
        return []

    coins = payload.get("coins") or []
    results: list[dict[str, Any]] = []
    for idx, coin in enumerate(coins[:_MAX_RESULTS]):
        symbol = (coin.get("symbol") or "").upper()
        name = coin.get("name") or symbol
        cg_id = coin.get("id") or coin.get("api_symbol")
        if not (symbol and cg_id):
            continue
        results.append({
            "symbol": symbol,
            "name": name,
            "asset_class": "crypto",
            "currency": "USD",
            "data_source": "coingecko",
            "data_source_id": cg_id,
            "thumb": coin.get("thumb") or coin.get("large") or None,
            "_orig_idx": idx,
        })

    _cache_set("crypto", query, results)
    return results


def _yfinance_search_sync(query: str) -> list[dict[str, Any]]:
    """Run yfinance.Search in a worker thread (it is a sync API)."""
    import yfinance as yf  # local import — heavy module
    s = yf.Search(query, max_results=_MAX_RESULTS, news_count=0)
    return list(s.quotes or [])


async def search_stocks(query: str) -> list[dict[str, Any]]:
    """Search Yahoo Finance (via the yfinance package) for equities / ETFs.

    Each result dict has same shape as `search_crypto` (thumb is None).
    Max 10 results.
    """
    cached = _cache_get("stocks", query)
    if cached is not None:
        return cached

    try:
        quotes = await asyncio.to_thread(_yfinance_search_sync, query)
    except Exception as e:  # noqa: BLE001
        logger.warning("yfinance search failed for %r: %s", query, e)
        return []

    results: list[dict[str, Any]] = []
    for idx, q in enumerate(quotes[:_MAX_RESULTS]):
        ticker = q.get("symbol")
        if not ticker:
            continue
        quote_type = (q.get("quoteType") or "").upper()
        # Skip non-tradeable + crypto (CoinGecko owns crypto)
        if quote_type in {"CURRENCY", "FUTURE", "OPTION", "INDEX", "CRYPTOCURRENCY"}:
            continue
        name = (
            q.get("longname")
            or q.get("shortname")
            or q.get("longName")
            or q.get("shortName")
            or ticker
        )
        exchange = q.get("exchange") or q.get("exchDisp")
        raw_currency = q.get("currency")
        # Yahoo Search payload often omits currency; infer from exchange.
        if not raw_currency:
            ex_up = (exchange or "").upper()
            if ex_up in {"SHH", "SHZ", "SHA", "SSE", "SZSE"}:
                currency = "CNY"
            elif ex_up == "HKG":
                currency = "HKD"
            else:
                currency = "USD"
        else:
            currency = raw_currency.upper()
        asset_class, market = _classify_stock(exchange, currency)
        results.append({
            "symbol": ticker.upper(),
            "name": name,
            "asset_class": asset_class,
            "currency": currency,
            "data_source": "yfinance",
            "data_source_id": ticker,
            "market": market,
            "thumb": None,
            "_orig_idx": idx,
        })

    _cache_set("stocks", query, results)
    return results


def _relevance_score(item: dict[str, Any], query: str) -> int:
    """Lower bucket = more relevant (used before falling back to API order)."""
    q = query.lower().strip()
    sym = (item.get("symbol") or "").lower()
    name = (item.get("name") or "").lower()
    # Only treat sym-equality as a top-rank signal when the query *looks* like
    # a ticker (≤5 chars). Otherwise scammers can hijack a generic name like
    # "ethereum" with a fake symbol "ETHEREUM" and outrank the real coin.
    if len(q) <= 5 and sym == q:
        return 0
    if sym.startswith(q) or name.startswith(q):
        return 1
    if q in sym or q in name:
        return 2
    return 3


async def search_assets(
    query: str,
    asset_class: str | None = None,
) -> list[dict[str, Any]]:
    """Combined search across crypto + stocks.

    `asset_class`: optional filter — "crypto" / "stock" / specific class
    ("us_stock", "eu_stock", "a_share"). When omitted, both backends are
    queried in parallel.
    """
    query = (query or "").strip()
    if len(query) < 2:
        return []

    want_crypto = asset_class in (None, "crypto")
    want_stock = asset_class in (None, "stock", "us_stock", "eu_stock", "a_share")

    tasks: list[asyncio.Task[list[dict[str, Any]]]] = []
    if want_crypto:
        tasks.append(asyncio.create_task(search_crypto(query)))
    if want_stock:
        tasks.append(asyncio.create_task(search_stocks(query)))

    if not tasks:
        return []

    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    merged: list[dict[str, Any]] = []
    for r in gathered:
        if isinstance(r, list):
            merged.extend(r)

    if asset_class and asset_class not in {"crypto", "stock"}:
        merged = [m for m in merged if m.get("asset_class") == asset_class]

    merged.sort(
        key=lambda m: (
            _relevance_score(m, query),
            m.get("_orig_idx", 999),
            m.get("symbol") or "",
        )
    )
    # Strip internal sort key before returning (shallow copy so cached entries
    # keep their _orig_idx for the next call).
    out: list[dict[str, Any]] = []
    for m in merged[: _MAX_RESULTS * 2]:
        clean = {k: v for k, v in m.items() if k != "_orig_idx"}
        out.append(clean)
    return out
