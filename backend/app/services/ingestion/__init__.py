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

import json

from app.core.config import get_settings
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
    llm_dispatched: int = 0  # rows queued for async LLM classification


def _fx_missing_meta(tx: Transaction, src: str, base: str) -> None:
    """Tag tx.metadata_json with fx_missing=True (non-destructive shallow merge)."""
    try:
        cur: dict = json.loads(tx.metadata_json) if tx.metadata_json else {}
        if not isinstance(cur, dict):
            cur = {}
    except (json.JSONDecodeError, TypeError):
        cur = {}
    cur.update({"fx_missing": True, "fx_src": src, "fx_base": base})
    tx.metadata_json = json.dumps(cur)


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

    # Step 1.5: multi-currency fold (FIX-13 / review V2 §V2-P0-1)
    # Populate fx_rate_to_base + base_amount for foreign-currency rows so the
    # COALESCE(...) in cashflow SQL actually folds to BASE_CURRENCY.
    settings = get_settings()
    base_currency = settings.base_currency.upper()
    from app.services.ingestion.fx import resolve_fx_to_base

    for tx in tx_list:
        tx_currency = (tx.currency or "").upper()
        if tx_currency == base_currency:
            continue  # SQL COALESCE handles same-currency via `amount`
        if tx.fx_rate_to_base is not None:
            continue  # caller / parser already provided the rate
        if tx.base_amount is not None:
            continue  # caller already provided base_amount
        rate = await resolve_fx_to_base(db, src_currency=tx_currency, base_currency=base_currency)
        if rate is not None:
            tx.fx_rate_to_base = rate
            tx.base_amount = (tx.amount if isinstance(tx.amount, Decimal) else Decimal(str(tx.amount))) * rate
        else:
            # Non-fatal: mark metadata so downstream cashflow can detect the gap
            _fx_missing_meta(tx, tx_currency, base_currency)

    # Step 2: auto-categorise (rule-matching). `transfer` rows are already
    # confirmed (no inbox), so we only run rules for income/expense.
    # For transfer rows pre-tagged by the parser (subaccount / cross-bank),
    # also assign a default transfer category so they don't sit in the inbox
    # when single-leg pairing fails.
    from app.services.transfer_matcher.engine import _resolve_transfer_category

    # Track which tx objects need to be routed to the L2 LLM after L1.
    # We dispatch async tasks AFTER db.flush() so each task gets a stable id.
    llm_target_txs: list[Transaction] = []

    if not skip_categorize:
        from app.services.categorizer.engine import categorize_transaction

        for tx in tx_list:
            if tx.type == "transfer":
                tx.is_pending = False
                if tx.category_id is None:
                    await _backfill_transfer_category(db, tx, _resolve_transfer_category)
                continue
            match = await categorize_transaction(db, tx)
            if match.matched and not match.requires_llm:
                # High confidence: L1 rule short-circuits.
                tx.is_pending = False
                tx.categorization_method = "rule"
                result.auto_categorized += 1
            else:
                # Either no rule matched, or the matched rule is "polluted"
                # (requires_llm=True). Route to LLM. Eligibility: pdf_import
                # / bank_api only — manual and mcp_agent rows already carry
                # the user's intent explicitly.
                if (tx.source or "") in {"pdf_import", "bank_api"}:
                    llm_target_txs.append(tx)
    else:
        # Even when categorisation is skipped, transfer rows shouldn't sit in
        # the inbox.
        for tx in tx_list:
            if tx.type == "transfer":
                tx.is_pending = False
                if tx.category_id is None:
                    await _backfill_transfer_category(db, tx, _resolve_transfer_category)

    await db.flush()

    # Step 2.5: schedule L2 LLM classification. Targets get a stable `tx.id`
    # only after the flush above. The LLM worker drains its queue from an
    # independent session, so it must only see rows that are already COMMITTED.
    #
    # V7-P1-4: previously we forced `await db.commit()` here, mid-pipeline, so
    # the worker could see the rows (V6-P1-6 race). But committing here broke
    # the caller's transaction boundary — if a later step (synthetic upgrade,
    # transfer matcher, recompute, or the route's own status update) failed,
    # the half-finished insert could no longer be rolled back. Reparse was the
    # worst case: old rows deleted + new rows committed before the status flip.
    #
    # Fix: keep everything (insert → categorise → pair → recompute) in ONE
    # transaction and defer the enqueue to a one-shot after_commit hook. The
    # rows are queued only once the caller's commit lands; a rollback fires
    # nothing, so the queue never points at rows that don't exist.
    # Only auto-dispatch L2 when the user opted into it (llm_auto_classify).
    # Default is OFF: unmatched rows just wait in the inbox until the user
    # clicks "AI 智能处理" (POST /llm/classify-inbox). L1 keyword matching
    # above always runs regardless.
    if llm_target_txs:
        from app.services import app_settings as _aps

        auto_classify = (await _aps.get_llm_settings(db)).auto_classify
        ids_to_classify = [tx.id for tx in llm_target_txs if tx.id is not None]
        if auto_classify and ids_to_classify:
            _enqueue_llm_after_commit(db, ids_to_classify)
            result.llm_dispatched += len(ids_to_classify)

    # Step 3: collect periods touched by the new batch BEFORE pairing — pairing
    # may add additional periods (counter-legs in different months).
    for tx in tx_list:
        period = parse_period(tx.occurred_at)
        if period:
            result.affected_periods.add(period)

    # Step 3.5: synthetic→real upgrade. Before the matcher runs, sweep new
    # rows against any existing synthetic mirrors in their account. If a new
    # real row matches a synthetic placeholder (same amount/currency/±N
    # days), retire the placeholder and re-point its paired source to the
    # real row. Without this, importing the missing PDF after manual bind
    # leaves 1 real + 1 synthetic + 1 new real = 3 rows for one transfer.
    if auto_pair:
        from app.services.transfer_matcher import replace_synthetic_with_real
        for tx in tx_list:
            if tx.id is None:
                continue
            await replace_synthetic_with_real(db, real_tx=tx)
        await db.flush()

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


async def _classify_one_with_llm(tx_id: int) -> None:
    """Run the L2 classifier on a single tx, in its own session.

    Imported lazily so the module load order doesn't break when LLM deps
    aren't installed. Failures are logged and swallowed — LLM is best-effort,
    the row stays in inbox if anything goes wrong.
    """
    try:
        from app.db import async_session_factory
        from app.services.cashflow.engine import recompute_for_periods, parse_period
        from app.services.llm.classifier import classify_with_llm

        async with async_session_factory() as session:
            tx = await session.get(Transaction, tx_id)
            if tx is None or tx.deleted_at is not None:
                return
            outcome = await classify_with_llm(session, tx)
            # If the LLM landed a high-confidence match, the row's category
            # changed → its month's cashflow snapshot must be refreshed.
            if outcome.matched:
                period = parse_period(tx.occurred_at)
                if period:
                    await recompute_for_periods(session, [period])
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("llm_classification_task_failed", tx_id=tx_id, error=str(exc)[:200])


def _enqueue_llm_after_commit(db: AsyncSession, tx_ids: list[int]) -> None:
    """Enqueue rows for paced L2 classification AFTER the caller commits.

    Registers a one-shot ``after_commit`` hook on the async session's
    underlying sync ``Session``. The queue worker drains one item every
    `llm_min_interval_sec` (≤ ~15 RPM, ERR-20260609-001) from its own
    session, so rows must be committed before they're enqueued.

    V7-P1-4: this replaces the old mid-pipeline ``db.commit()`` so ingestion
    stays a single atomic transaction. The ``fired`` guard makes it idempotent
    if the session commits more than once during the request. We don't remove
    the listener inside its own dispatch (that would mutate the listener
    collection mid-iteration); the per-request session is discarded at request
    end, so the listener can't leak across requests anyway.

    V8-P2-1: also listen for ``after_rollback``. If THIS ingestion's
    transaction rolls back, the rows are gone — mark cancelled so a later
    commit on a reused session (service/test/script that rolls back then
    commits other work) doesn't enqueue ids that no longer exist.
    """
    from sqlalchemy import event

    from app.services.llm import queue as llm_queue

    sync_session = db.sync_session
    state = {"fired": False, "cancelled": False}

    def _after_commit(_session) -> None:
        if state["fired"] or state["cancelled"]:
            return
        state["fired"] = True
        added = llm_queue.enqueue(list(tx_ids))
        logger.info("llm_enqueued", requested=len(tx_ids), added=added)

    def _after_rollback(_session) -> None:
        state["cancelled"] = True

    event.listen(sync_session, "after_commit", _after_commit)
    event.listen(sync_session, "after_rollback", _after_rollback)


async def _backfill_transfer_category(db: AsyncSession, tx: Transaction, resolver) -> None:
    """Set `tx.category_id` based on the parser's transfer hint.

    Subaccount-tagged rows → 内部储蓄. Anything else (cross-bank hint, no hint)
    → 跨行划转. Only assigns when category_id is NULL — pre-existing categories
    win. Used for single-leg transfers that can't be paired (so the matcher's
    `mark_subaccount_pair` / `pair_transactions` paths never fire).
    """
    try:
        meta: dict = json.loads(tx.metadata_json) if tx.metadata_json else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    is_subaccount = isinstance(meta, dict) and meta.get("subaccount") is True
    kind = "subaccount" if is_subaccount else "cross_bank"
    cat_id = await resolver(db, kind=kind)
    if cat_id is not None:
        tx.category_id = cat_id


async def recompute_after_delete(
    db: AsyncSession,
    deleted_periods: Iterable[tuple[int, int] | None],
) -> int:
    """Helper for delete paths: dedupe periods + run recompute_for_periods.

    Caller commits afterwards.
    """
    return await recompute_for_periods(db, deleted_periods)
