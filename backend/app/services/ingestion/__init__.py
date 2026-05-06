"""Unified transaction ingestion pipeline (Sprint 1 FIX-4).

Every write path that creates `Transaction` rows — PDF upload / reparse,
manual API create / batch, bank API sync, MCP `add_transaction` — must go
through `ingest_transactions` so the following invariants are guaranteed:

1. **Amount sign normalisation**: non-`adjustment` rows always store
   `ABS(amount)` (direction is encoded in `type` + `metadata.transfer_direction`,
   never the sign). `adjustment` keeps its signed delta.
2. **Auto-categorisation**: rows whose `description` matches a
   `categorization_rule` get `category_id` set and `is_pending=False`. Rows
   already labelled `transfer` skip the inbox too.
3. **Cross-account / sub-account pairing** (when `auto_pair=True`): the
   transfer_matcher runs against the new batch, tagging both legs with
   `transfer_direction` so the balance view applies correct signs.
4. **Cashflow recompute**: every month touched by the new rows AND every
   month touched by paired counter-legs gets its snapshot rewritten.

Callers must `db.add()` the `Transaction` objects beforehand and `commit()`
afterwards. This service runs `db.flush()` between steps so generated IDs
are visible to the matcher.

Review V1 cross-references: §P1-3 (stale cashflow on PDF flows), §P1-5
(amount sign drift), §P1-7 (bank_sync bypassing the pipeline).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Transaction
from app.services.cashflow.engine import parse_period, recompute_for_periods

logger = structlog.get_logger(__name__)


@dataclass
class IngestResult:
    total: int = 0
    auto_categorized: int = 0
    auto_paired: list[tuple[int, int]] = field(default_factory=list)
    subaccount_paired: list[tuple[int, int]] = field(default_factory=list)
    suggested: list[dict] = field(default_factory=list)
    affected_periods: set[tuple[int, int]] = field(default_factory=set)


def _normalize_amount(tx: Transaction) -> None:
    """Force `ABS(amount)` for everything except `adjustment` rows."""
    if tx.type == "adjustment":
        return
    if tx.amount is None:
        return
    amount = tx.amount if isinstance(tx.amount, Decimal) else Decimal(str(tx.amount))
    if amount < 0:
        tx.amount = -amount
    else:
        tx.amount = amount


async def ingest_transactions(
    db: AsyncSession,
    txs: Iterable[Transaction],
    *,
    auto_pair: bool = True,
    skip_categorize: bool = False,
) -> IngestResult:
    """Run the unified ingestion pipeline over `txs`.

    Args:
        db: active async session.
        txs: Transaction instances already attached via `db.add()`. Caller
            commits afterwards.
        auto_pair: run the cross-account / same-account pairing matcher.
            PDF upload / bank_sync want True; single-row manual create can
            disable to avoid a full-table scan when the batch is just one row.
        skip_categorize: PDF parsers that pre-classified rows (e.g. Revolut
            column-aware) may want to keep their pre-set `category_id` /
            `is_pending=False` instead of re-running rule matching.

    Returns:
        IngestResult — caller can log counts / surface to UI.
    """
    tx_list: list[Transaction] = list(txs)
    result = IngestResult(total=len(tx_list))
    if not tx_list:
        return result

    # Step 1: amount sign normalisation
    for tx in tx_list:
        _normalize_amount(tx)

    # Step 2: auto-categorise (rule-matching). `transfer` rows are already
    # confirmed (no inbox), so we only run rules for income/expense.
    if not skip_categorize:
        from app.services.categorizer.engine import categorize_transaction

        for tx in tx_list:
            if tx.type == "transfer":
                tx.is_pending = False
                continue
            matched = await categorize_transaction(db, tx)
            if matched:
                tx.is_pending = False
                result.auto_categorized += 1
    else:
        # Even when categorisation is skipped, transfer rows shouldn't sit in
        # the inbox.
        for tx in tx_list:
            if tx.type == "transfer":
                tx.is_pending = False

    await db.flush()

    # Step 3: collect periods touched by the new batch BEFORE pairing — pairing
    # may add additional periods (counter-legs in different months).
    for tx in tx_list:
        period = parse_period(tx.occurred_at)
        if period:
            result.affected_periods.add(period)

    # Step 4: cross-account / sub-account pairing
    if auto_pair:
        from app.services.transfer_matcher import auto_pair_after_import

        new_ids = [tx.id for tx in tx_list if tx.id is not None]
        if new_ids:
            try:
                pair_summary = await auto_pair_after_import(db, new_ids)
                result.auto_paired = pair_summary.get("auto_paired", [])
                result.subaccount_paired = pair_summary.get("subaccount_paired", [])
                result.suggested = pair_summary.get("suggested", [])

                # Counter-legs may live in months not in the original batch —
                # collect their periods too.
                paired_counter_ids: set[int] = set()
                for a, b in result.auto_paired:
                    paired_counter_ids.update({a, b})
                for a, b in result.subaccount_paired:
                    paired_counter_ids.update({a, b})
                if paired_counter_ids:
                    from sqlalchemy import select

                    rows = (await db.execute(
                        select(Transaction.occurred_at).where(Transaction.id.in_(paired_counter_ids))
                    )).all()
                    for (occ,) in rows:
                        period = parse_period(occ)
                        if period:
                            result.affected_periods.add(period)
            except Exception as e:
                # Matcher failure is non-fatal — surface in logs but let the
                # rows persist. The user can re-run via /transfers/suggestions.
                logger.warning("transfer_matcher_failed", error=str(e))

    # Step 5: refresh cashflow snapshots for every affected period
    if result.affected_periods:
        await recompute_for_periods(db, result.affected_periods)

    logger.info(
        "ingest_complete",
        total=result.total,
        auto_categorized=result.auto_categorized,
        auto_paired=len(result.auto_paired),
        subaccount_paired=len(result.subaccount_paired),
        suggested=len(result.suggested),
        periods=len(result.affected_periods),
    )
    return result


async def recompute_after_delete(
    db: AsyncSession,
    deleted_periods: Iterable[tuple[int, int] | None],
) -> int:
    """Helper for delete paths: dedupe periods + run recompute_for_periods.

    Caller commits afterwards.
    """
    return await recompute_for_periods(db, deleted_periods)
