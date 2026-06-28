"""Portfolio *composition* — the third distribution view (besides by-class and
by-currency). Unlike ``portfolio_breakdown`` (investments only), this folds
**cash + investments** into one picture of "what individual things make up my
net worth", per the product rules:

- Cash accounts grouped by currency (all EUR across accounts → one ``EUR 现金``).
- Stablecoins (USDT/USDC/DAI/…) merged into one ``USD 稳定币`` bucket.
- Crypto / stocks grouped by symbol, so the same coin across exchanges sums
  (BTC on Bitget + BTC on Binance → one ``BTC``).
- Dust < ``_DUST`` (€0.1) is dropped entirely.
- A holding ≥ dust but < ``_SMALL`` (€20) is folded into a per-category
  "small" bucket (``小额股票`` / ``小额加密货币`` / …) so the chart isn't
  swamped by long-tail positions. Cash & stablecoin buckets are exempt.

Thresholds are in BASE currency units; calibrated for an EUR base (this
deployment). Money math reuses ``convert_to_base`` and the same account /
holding filters as ``compute_net_worth`` so totals reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Asset, AssetHolding, MarketPrice
from app.services.valuation.fx import convert_to_base

# USD-pegged stablecoins collapse into one bucket (mirrors the FX aliasing in
# services/valuation/fx + MCP _convert_fx).
_STABLECOINS = frozenset({"USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX", "USDD", "PYUSD", "USDE"})

_DUST = Decimal("0.1")    # below this (base ccy) → dropped as junk
_SMALL = Decimal("20")    # [_DUST, _SMALL) investment → "小额X" bucket

# Per-category label for the "small" bucket.
_SMALL_LABELS: dict[str, str] = {
    "us_stock": "小额股票",
    "eu_stock": "小额股票",
    "a_share": "小额股票",
    "crypto": "小额加密货币",
    "fund": "小额基金",
    "bond": "小额债券",
    "gold": "小额黄金",
}


def _small_label(asset_class: str) -> str:
    return _SMALL_LABELS.get(asset_class, "小额其他")


@dataclass
class _Bucket:
    label: str
    asset_class: str
    value: Decimal = Decimal("0")
    count: int = 0


@dataclass
class CompositionResult:
    base_currency: str
    total: Decimal
    entries: list[dict] = field(default_factory=list)
    dust_excluded_count: int = 0


async def compute_composition(db: AsyncSession, base_currency: str) -> CompositionResult:
    buckets: dict[str, _Bucket] = {}

    # ─── 1) Cash legs (one bucket per currency; snapshot accounts excluded) ──
    cash_rows = (await db.execute(text("""
        SELECT v.currency, SUM(v.balance) AS total
        FROM v_account_balance v
        JOIN accounts a ON a.id = v.account_id
        WHERE a.include_in_total = 1 AND a.deleted_at IS NULL
          AND a.type NOT IN ('brokerage', 'crypto_wallet', 'exchange')
        GROUP BY v.currency
    """))).all()
    for currency, total in cash_rows:
        original = total if isinstance(total, Decimal) else Decimal(str(total or 0))
        if original == 0:
            continue
        converted = (
            original if currency == base_currency
            else await convert_to_base(db, original, currency, base_currency)
        )
        if converted is None:
            continue
        key = f"cash:{currency}"
        buckets[key] = _Bucket(label=f"{currency} 现金", asset_class="cash",
                               value=converted, count=1)

    # ─── 2) Investment holdings (grouped by logical asset) ──────────────────
    inv_rows = (await db.execute(
        select(AssetHolding, Asset)
        .join(Asset, AssetHolding.asset_id == Asset.id)
        .join(Account, Account.id == AssetHolding.account_id)
        .where(
            Account.include_in_total == True,  # noqa: E712
            Account.deleted_at.is_(None),
            AssetHolding.is_active == True,  # noqa: E712
        )
    )).all()
    for holding, asset in inv_rows:
        latest = (await db.execute(
            select(MarketPrice).where(MarketPrice.asset_id == asset.id)
            .order_by(MarketPrice.quoted_at.desc()).limit(1)
        )).scalar_one_or_none()
        if latest is None:
            continue
        original_value = holding.quantity * latest.price
        converted = (
            original_value if latest.currency == base_currency
            else await convert_to_base(db, original_value, latest.currency, base_currency)
        )
        if converted is None:
            continue

        symbol = (asset.symbol or asset.name or "?").upper()
        asset_class = asset.asset_class or "other"
        if symbol in _STABLECOINS:
            key, label, cls = "stable", "USD 稳定币", "stable"
        else:
            key = f"{asset_class}:{symbol}"
            label = asset.symbol or asset.name or symbol
            cls = asset_class
        b = buckets.setdefault(key, _Bucket(label=label, asset_class=cls))
        b.value += converted
        b.count += 1

    # ─── 3) Dust filter + small-bucket folding (after per-asset aggregation) ─
    final: dict[str, _Bucket] = {}
    dust = 0
    for b in buckets.values():
        if b.value < _DUST:
            dust += 1
            continue
        # Cash & stablecoins are never "junk-bucketed"; investments below
        # _SMALL fold into a per-category small bucket.
        if b.asset_class in ("cash", "stable") or b.value >= _SMALL:
            final[b.label] = _merge(final.get(b.label), b)
        else:
            label = _small_label(b.asset_class)
            final[label] = _merge(
                final.get(label),
                _Bucket(label=label, asset_class="small", value=b.value, count=b.count),
            )

    total = sum((b.value for b in final.values()), Decimal("0"))
    entries = [
        {"key": b.label, "label": b.label, "asset_class": b.asset_class,
         "value": str(b.value), "count": b.count}
        for b in sorted(final.values(), key=lambda x: x.value, reverse=True)
    ]
    return CompositionResult(
        base_currency=base_currency, total=total, entries=entries,
        dust_excluded_count=dust,
    )


def _merge(existing: _Bucket | None, incoming: _Bucket) -> _Bucket:
    if existing is None:
        return incoming
    existing.value += incoming.value
    existing.count += incoming.count
    return existing
