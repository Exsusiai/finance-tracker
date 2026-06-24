"""P1-4 A4.2: per-account wallet/exchange sync orchestrator.

One entry point — ``sync_account(db, account_id, alchemy_api_key)`` —
that picks the right path based on ``Account.type``:

- ``crypto_wallet`` → loops over ``chain_addresses`` rows, dispatches to
  the matching on-chain provider, applies the snapshot per (account,
  chain).
- ``exchange`` → loops over ``exchange_connections`` rows, decrypts the
  AES-GCM blobs, dispatches to the matching CEX provider, applies the
  snapshot with ``chain=""``.

Per-source failures are *captured*, not raised: one rate-limited chain
shouldn't kill the rest. The summary returns one ``SyncResult`` per
source so the UI can show partial results.

Indirection layer: ``_dispatch_chain`` / ``_dispatch_exchange`` exist
purely so tests can monkeypatch them without touching the real
provider modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    Asset,
    AssetHolding,
    BrokerConnection,
    ChainAddress,
    ExchangeConnection,
    MarketPrice,
    _utcnow_str,
)
from app.services.bank_sync.crypto import decrypt_str
from app.services.broker_sync import dispatch as _broker_dispatch
from app.services.broker_sync.upsert import apply_broker_snapshot
from app.services.crypto_sync import dispatch as _crypto_dispatch
from app.services.exchange_sync import dispatch as _exchange_dispatch
from app.services.market_data.coingecko import (
    fetch_native_price,
    fetch_native_prices,
    fetch_token_prices,
)
from app.services.wallet_sync.spam_filter import is_spam_token
from app.services.wallet_sync.upsert import apply_balance_snapshot

log = structlog.get_logger(__name__)


# Patterns that may carry secrets into an upstream error message. Used by
# `_safe_error_text` to scrub before persisting to `last_sync_error` (DB +
# echoed to UI). The Alchemy API key in particular sits as the LAST
# path segment of every URL.
_ALCHEMY_KEY_RE = re.compile(r"/v2/[A-Za-z0-9_-]{20,}")
_BINANCE_SIG_RE = re.compile(r"signature=[A-Za-z0-9]+")
# IBKR Flex token rides as `t=<token>` in the SendRequest/GetStatement URL.
_FLEX_TOKEN_RE = re.compile(r"([?&]t=)[A-Za-z0-9]+")


def _safe_error_text(exc: Exception, *, max_len: int = 250) -> str:
    """Return a UI-safe one-liner for an exception.

    Strategy:
    - For httpx.HTTPStatusError, only the status code (URL contains secrets).
    - For httpx network errors, the class name + first 100 chars of repr.
    - For everything else, ``str(exc)`` truncated, with known secret
      patterns scrubbed defensively. If ``str(exc)`` is empty, fall back
      to the exception class name.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"{exc.__class__.__name__}: upstream connection error"
    msg = str(exc) or exc.__class__.__name__
    msg = _ALCHEMY_KEY_RE.sub("/v2/<redacted>", msg)
    msg = _BINANCE_SIG_RE.sub("signature=<redacted>", msg)
    msg = _FLEX_TOKEN_RE.sub(r"\1<redacted>", msg)
    return msg[:max_len]


# Indirection — tests monkeypatch these.
def _dispatch_chain(chain: str, alchemy_api_key: str | None):
    return _crypto_dispatch(chain, alchemy_api_key)


def _dispatch_exchange(exchange: str):
    return _exchange_dispatch(exchange)


def _dispatch_broker(provider: str, *, token: str, query_id: str):
    return _broker_dispatch(provider, token=token, query_id=query_id)


@dataclass
class SyncResult:
    label: str
    chain: str | None
    exchange: str | None
    synced: int
    error: str | None = None


@dataclass
class SyncSummary:
    account_id: int
    account_type: str
    results: list[SyncResult] = field(default_factory=list)

    @property
    def total_synced(self) -> int:
        return sum(r.synced for r in self.results)

    @property
    def total_errors(self) -> int:
        return sum(1 for r in self.results if r.error)


async def sync_account(
    db: AsyncSession,
    account_id: int,
    *,
    alchemy_api_key: str | None,
) -> SyncSummary:
    acc = (
        await db.execute(
            select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if acc is None:
        raise ValueError(f"Account {account_id} not found or deleted")
    if acc.type not in ("crypto_wallet", "exchange", "brokerage"):
        raise ValueError(
            f"Account {account_id} is of type {acc.type!r}; sync only supports "
            "'crypto_wallet', 'exchange' and 'brokerage'."
        )

    summary = SyncSummary(account_id=acc.id, account_type=acc.type)

    if acc.type == "crypto_wallet":
        await _sync_crypto_wallet(db, acc, alchemy_api_key, summary)
    elif acc.type == "exchange":
        await _sync_exchange(db, acc, summary)
    else:
        # brokerage — the Flex statement carries its own prices, so no
        # CoinGecko refresh is needed (or wanted) afterwards.
        await _sync_brokerage(db, acc, summary)
        return summary

    # Price refresh runs last and best-effort — a CoinGecko outage must
    # not roll back the holdings rows we just wrote. Crypto/CEX only:
    # brokerage prices come straight from the statement.
    try:
        await _refresh_prices_for_account(db, acc.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("wallet_sync_price_refresh_failed", account_id=acc.id, error=str(exc))

    return summary


# ─── crypto_wallet ─────────────────────────────────────────────────────────


async def _sync_crypto_wallet(
    db: AsyncSession,
    acc: Account,
    alchemy_api_key: str | None,
    summary: SyncSummary,
) -> None:
    rows: Iterable[ChainAddress] = (
        await db.execute(
            select(ChainAddress).where(ChainAddress.account_id == acc.id)
        )
    ).scalars().all()

    now = _utcnow_str()
    for row in rows:
        label = f"{row.chain}:{row.address[:8]}…"
        try:
            provider = _dispatch_chain(row.chain, alchemy_api_key)
            items = await provider.fetch_balances(row.address)
            synced = await apply_balance_snapshot(db, acc.id, row.chain, items)
            row.last_synced_at = now
            row.last_sync_status = "ok"
            row.last_sync_error = None
            summary.results.append(SyncResult(
                label=label, chain=row.chain, exchange=None,
                synced=synced, error=None,
            ))
        except Exception as exc:  # noqa: BLE001 — capture & continue is intentional
            # Server-side log keeps full repr for debugging; UI / DB
            # gets the scrubbed version so an Alchemy API key embedded
            # in an httpx URL can't leak via `last_sync_error`.
            log.warning(
                "wallet_sync_chain_failed",
                account_id=acc.id, chain=row.chain,
                # V5-P1-6: never log raw repr(exc) — httpx HTTPStatusError's
                # repr contains the request URL (Alchemy key, Binance
                # signature, etc.). _safe_error_text scrubs these.
                error_class=exc.__class__.__name__, error=_safe_error_text(exc),
            )
            safe = _safe_error_text(exc)
            row.last_synced_at = now
            row.last_sync_status = "error"
            row.last_sync_error = safe
            summary.results.append(SyncResult(
                label=label, chain=row.chain, exchange=None,
                synced=0, error=safe,
            ))


# ─── exchange ──────────────────────────────────────────────────────────────


async def _sync_exchange(
    db: AsyncSession,
    acc: Account,
    summary: SyncSummary,
) -> None:
    rows: Iterable[ExchangeConnection] = (
        await db.execute(
            select(ExchangeConnection).where(ExchangeConnection.account_id == acc.id)
        )
    ).scalars().all()

    now = _utcnow_str()
    for row in rows:
        label = row.exchange
        try:
            api_key = decrypt_str(row.api_key_enc)
            api_secret = decrypt_str(row.api_secret_enc)
            passphrase = decrypt_str(row.api_passphrase_enc) if row.api_passphrase_enc else None
            provider = _dispatch_exchange(row.exchange)
            items = await provider.fetch_balances(
                api_key=api_key, api_secret=api_secret, passphrase=passphrase
            )
            synced = await apply_balance_snapshot(db, acc.id, "", items)
            row.last_synced_at = now
            row.last_sync_status = "ok"
            row.last_sync_error = None
            summary.results.append(SyncResult(
                label=label, chain=None, exchange=row.exchange,
                synced=synced, error=None,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "wallet_sync_exchange_failed",
                account_id=acc.id, exchange=row.exchange,
                # V5-P1-6: never log raw repr(exc) — httpx HTTPStatusError's
                # repr contains the request URL (Alchemy key, Binance
                # signature, etc.). _safe_error_text scrubs these.
                error_class=exc.__class__.__name__, error=_safe_error_text(exc),
            )
            safe = _safe_error_text(exc)
            row.last_synced_at = now
            row.last_sync_status = "error"
            row.last_sync_error = safe
            summary.results.append(SyncResult(
                label=label, chain=None, exchange=row.exchange,
                synced=0, error=safe,
            ))


# ─── brokerage ───────────────────────────────────────────────────────────────


async def _sync_brokerage(
    db: AsyncSession,
    acc: Account,
    summary: SyncSummary,
) -> None:
    rows: Iterable[BrokerConnection] = (
        await db.execute(
            select(BrokerConnection).where(BrokerConnection.account_id == acc.id)
        )
    ).scalars().all()

    now = _utcnow_str()
    for row in rows:
        label = row.provider
        try:
            token = decrypt_str(row.token_enc)
            provider = _dispatch_broker(row.provider, token=token, query_id=row.query_id)
            positions = await provider.fetch_positions()
            synced = await apply_broker_snapshot(db, acc.id, positions, source=row.provider)
            row.last_synced_at = now
            row.last_sync_status = "ok"
            row.last_sync_error = None
            summary.results.append(SyncResult(
                label=label, chain=None, exchange=None,
                synced=synced, error=None,
            ))
        except Exception as exc:  # noqa: BLE001 — capture & continue is intentional
            log.warning(
                "wallet_sync_broker_failed",
                account_id=acc.id, provider=row.provider,
                # Never log raw repr(exc) — an httpx URL would carry the
                # Flex token in its query string. _safe_error_text scrubs it.
                error_class=exc.__class__.__name__, error=_safe_error_text(exc),
            )
            safe = _safe_error_text(exc)
            row.last_synced_at = now
            row.last_sync_status = "error"
            row.last_sync_error = safe
            summary.results.append(SyncResult(
                label=label, chain=None, exchange=None,
                synced=0, error=safe,
            ))


# ─── Price refresh ─────────────────────────────────────────────────────────


async def _refresh_prices_for_account(db: AsyncSession, account_id: int) -> None:
    """Look up CoinGecko prices for every Asset this account currently
    holds and write them as fresh ``market_prices`` rows.

    Strategy:
    - Group active holdings by (chain, contract). Tokens with a contract
      go through the per-chain batched ``token_price`` endpoint; tokens
      without a contract (native + CEX) go through ``simple/price`` by
      symbol.
    - Skip anything `is_spam_token()` flags (defence in depth — should
      never fire here since upsert.py already filtered, but cheap).
    - Best-effort: a per-symbol failure logs + moves on.
    """
    rows = (
        await db.execute(
            select(AssetHolding, Asset)
            .join(Asset, Asset.id == AssetHolding.asset_id)
            .where(
                AssetHolding.account_id == account_id,
                AssetHolding.is_active == True,  # noqa: E712
            )
        )
    ).all()
    if not rows:
        return

    now = _utcnow_str()

    # Per-(chain) batched token lookups.
    by_chain: dict[str, list[tuple[Asset, str]]] = {}
    by_native_symbol: dict[str, list[Asset]] = {}
    for holding, asset in rows:
        if is_spam_token(asset.symbol, asset.name):
            continue
        # On-chain token has a contract → batched lookup by chain.
        if asset.data_source == "onchain" and asset.data_source_id and holding.chain:
            by_chain.setdefault(holding.chain, []).append((asset, asset.data_source_id))
        else:
            # Native chain coin or CEX symbol — look up by ticker.
            by_native_symbol.setdefault((asset.symbol or "").upper(), []).append(asset)

    # Dedupe across the whole refresh: the same Asset row often shows up
    # on multiple chains (e.g. ETH-native on both Ethereum and Arbitrum
    # point to the same Asset id since we key by symbol). Writing two
    # MarketPrice rows with identical (asset_id, source, quoted_at) hits
    # the unique constraint and rolls back the entire sync session.
    written_asset_ids: set[int] = set()

    def _record(asset_id: int, price: Decimal) -> None:
        if asset_id in written_asset_ids:
            return
        written_asset_ids.add(asset_id)
        db.add(
            MarketPrice(
                asset_id=asset_id,
                quoted_at=now,
                price=price,
                currency="USDT",
                source="coingecko",
            )
        )

    # 1) Batched contract lookups, one HTTP call per chain.
    for chain, items in by_chain.items():
        contracts = [c for _, c in items]
        prices = await fetch_token_prices(chain=chain, contracts=contracts)
        for asset, contract in items:
            price = prices.get(contract)
            if price is None:
                continue
            _record(asset.id, price)

    # 2) Native + CEX lookups: ONE batched CoinGecko call for all
    #    distinct tickers (saves N round-trips when an account holds
    #    many native symbols, e.g. BTC + ETH + SOL + BNB + ...).
    distinct_symbols = [s for s in by_native_symbol.keys() if s]
    if distinct_symbols:
        prices_by_symbol = await fetch_native_prices(distinct_symbols)
        for symbol, price in prices_by_symbol.items():
            for asset in by_native_symbol.get(symbol, []):
                _record(asset.id, price)

    await db.flush()
