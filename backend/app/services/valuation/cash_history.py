"""Historical cash-assets curve, reconstructed from the ledger.

Cash assets at a month-end = Σ over non-snapshot, included accounts of
(initial_balance + signed transaction amounts up to that month), each currency
bucket converted to base with the LATEST FX — the same methodology as
``net_worth``'s cash leg, so the most recent point equals
``NetWorthResult.cash_total``.

Unlike portfolio market value (no historical holding quantities), cash history
IS reconstructable: we have the full transaction ledger.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.valuation.fx import convert_to_base

# Per-row sign, mirroring v_account_balance (main.py _BALANCE_VIEW_SQL). Kept
# in ACCOUNT currency — folding happens per currency bucket with the latest
# rate, matching net_worth (which reads v_account_balance then converts).
_SIGNED_AMOUNT = """
    CASE
        WHEN json_valid(t.metadata_json) AND json_extract(t.metadata_json, '$.subaccount') = 1 THEN 0
        WHEN t.type = 'transfer' AND json_valid(t.metadata_json)
             AND json_extract(t.metadata_json, '$.transfer_direction') = 'in'  THEN  ABS(t.amount)
        WHEN t.type = 'transfer' AND json_valid(t.metadata_json)
             AND json_extract(t.metadata_json, '$.transfer_direction') = 'out' THEN -ABS(t.amount)
        WHEN t.type = 'transfer'   THEN -ABS(t.amount)
        WHEN t.type = 'expense'    THEN -ABS(t.amount)
        WHEN t.type = 'income'     THEN  ABS(t.amount)
        WHEN t.type = 'adjustment' THEN  t.amount
        ELSE 0
    END
"""

# Non-snapshot accounts that count toward total — same set as net_worth's cash
# leg. (Brokerage / crypto / exchange are valued via holdings, not cash.)
_CASH_ACCOUNT_FILTER = (
    "a.deleted_at IS NULL AND a.include_in_total = 1 "
    "AND a.type NOT IN ('brokerage', 'crypto_wallet', 'exchange')"
)


async def compute_cash_history(db: AsyncSession, base_currency: str) -> list[tuple[str, str]]:
    """Return ``[(period 'YYYY-MM', cash_total_base)]`` ascending, one entry per
    month that had any cash-account activity, as a running real balance."""
    # Opening balance per currency (before any transaction).
    opening: dict[str, Decimal] = {}
    for currency, total in (await db.execute(text(f"""
        SELECT currency, SUM(initial_balance) FROM accounts a
        WHERE {_CASH_ACCOUNT_FILTER}
        GROUP BY currency
    """))).all():
        opening[currency] = Decimal(str(total or 0))

    # Per (currency, month) signed flow in account currency. No is_pending
    # filter — v_account_balance includes pending rows, so we match it.
    flow: dict[tuple[str, str], Decimal] = {}
    periods: set[str] = set()
    for currency, period, f in (await db.execute(text(f"""
        SELECT a.currency, substr(t.occurred_at, 1, 7) AS period, SUM({_SIGNED_AMOUNT}) AS flow
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.deleted_at IS NULL AND {_CASH_ACCOUNT_FILTER}
        GROUP BY a.currency, period
    """))).all():
        flow[(currency, period)] = Decimal(str(f or 0))
        periods.add(period)

    currencies = set(opening) | {c for c, _ in flow}
    running = {c: opening.get(c, Decimal("0")) for c in currencies}

    out: list[tuple[str, str]] = []
    for p in sorted(periods):
        for c in currencies:
            running[c] += flow.get((c, p), Decimal("0"))
        total = Decimal("0")
        for c, bal in running.items():
            conv = await convert_to_base(db, bal, c, base_currency)
            if conv is not None:
                total += conv
        out.append((p, str(total)))
    return out
