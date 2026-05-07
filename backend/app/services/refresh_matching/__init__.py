"""Global "refresh matching" pipeline.

Each step is a top-level async function that mutates `RefreshContext`. The
public `run_full_pipeline(ctx)` orchestrator just calls them in order. This
shape lets each step be unit-tested in isolation, and lets the route in
`api/v1/system.py` stay thin.

Mental model: "pretend every PDF was just re-imported." Steps re-run all
detection / classification / pairing logic and re-enqueue anything the
user hasn't manually touched (source=manual or non-empty user_note).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Transaction
from app.services.cashflow.engine import recompute_for_periods
from app.services.categorizer.engine import categorize_transaction
from app.services.pdf_parser.engine import (
    _classify_transfer,
    _reset_subaccount_names,
    _set_subaccount_names,
)
from app.services.transfer_matcher import (
    SCORE_THRESHOLD_AUTO,
    detect_same_account_pairs,
    detect_single_leg_iban,
    find_transfer_pairs,
    mark_subaccount_pair,
    pair_orphan_single_legs,
    pair_transactions,
)
from app.services.transfer_matcher.engine import (
    _merge_meta,
    _resolve_transfer_category,
)


SUMMARY_KEYS = (
    "orphan_pointers_cleared",
    "type_promoted_to_transfer",
    "recategorized",
    "subaccount_pairs",
    "single_leg_iban",
    "auto_paired",
    "orphan_paired",
    "subaccount_orphans_categorized",
    "reenqueued_to_inbox",
    "periods_recomputed",
)


@dataclass
class RefreshContext:
    db: AsyncSession
    summary: dict[str, int] = field(default_factory=lambda: {k: 0 for k in SUMMARY_KEYS})
    affected_periods: set[tuple[int, int]] = field(default_factory=set)

    def track_period(self, occurred_at: str | None) -> None:
        from app.services.cashflow.engine import parse_period
        if not occurred_at:
            return
        p = parse_period(occurred_at)
        if p is not None:
            self.affected_periods.add(p)


# ─── Helpers ────────────────────────────────────────────────────────────


def _safe_meta(metadata_json: str | None) -> dict:
    """Robust JSON-object load that never raises."""
    if not metadata_json:
        return {}
    try:
        m = json.loads(metadata_json) or {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return m if isinstance(m, dict) else {}


def _dump_meta(meta: dict) -> str | None:
    return json.dumps(meta, sort_keys=True, ensure_ascii=False) if meta else None


# ─── Step "-1": orphan-pointer cleanup ──────────────────────────────────


async def step_clear_orphan_pointers(ctx: RefreshContext) -> None:
    """Clear `counter_account_id` + `paired_with_tx_id` on rows whose
    paired counterpart is missing (deleted, or never written). Backfills
    the metadata back-pointer when the counterpart actually exists but
    `paired_with_tx_id` was never recorded — historically `pair_transactions`
    didn't write it, which made every refresh-matching unpair confirmed
    candidate pairs.
    """
    pointer_rows = (await ctx.db.execute(
        select(Transaction).where(
            Transaction.deleted_at.is_(None),
            Transaction.counter_account_id.isnot(None),
        )
    )).scalars().all()
    for r in pointer_rows:
        meta = _safe_meta(r.metadata_json)
        paired_id = meta.get("paired_with_tx_id")

        # Path A: explicit pointer present → just verify alive.
        if paired_id is not None:
            counterpart_alive = (await ctx.db.execute(
                select(Transaction.id).where(
                    Transaction.id == paired_id,
                    Transaction.deleted_at.is_(None),
                )
            )).scalar_one_or_none() is not None
            if counterpart_alive:
                continue
        else:
            # Path B: pointer missing but counter_account_id set. Look in
            # the counter account for a row that mutually points back at us
            # (counter_account_id == our account_id) — that's our paired
            # counterpart written without the back-pointer (legacy
            # pair_transactions). Backfill the pointer instead of clearing.
            mate = (await ctx.db.execute(
                select(Transaction).where(
                    Transaction.account_id == r.counter_account_id,
                    Transaction.counter_account_id == r.account_id,
                    Transaction.deleted_at.is_(None),
                    Transaction.id != r.id,
                ).limit(1)
            )).scalars().first()
            if mate is not None:
                meta["paired_with_tx_id"] = mate.id
                r.metadata_json = _dump_meta(meta)
                # Also patch the mate's metadata if its pointer is missing
                mate_meta = _safe_meta(mate.metadata_json)
                if mate_meta.get("paired_with_tx_id") is None:
                    mate_meta["paired_with_tx_id"] = r.id
                    mate.metadata_json = _dump_meta(mate_meta)
                continue

        # Genuine orphan — clear the stale pointer
        r.counter_account_id = None
        meta.pop("paired_with_tx_id", None)
        meta.pop("synthetic_counterleg", None)
        r.metadata_json = _dump_meta(meta)
        ctx.summary["orphan_pointers_cleared"] += 1
        ctx.track_period(r.occurred_at)
    await ctx.db.flush()


# ─── Step 0: type re-detection ─────────────────────────────────────────


async def step_redetect_type(ctx: RefreshContext) -> None:
    """Re-run `_classify_transfer` on raw_description for pdf_import rows
    the user hasn't touched. Promotes expense/income → transfer when a
    sub-account name or cross-bank cue matches.
    """
    accts = (await ctx.db.execute(
        select(Account).where(Account.deleted_at.is_(None))
    )).scalars().all()
    sub_names_by_account: dict[int, list[str]] = {}
    for a in accts:
        names: list[str] = []
        meta = _safe_meta(a.metadata_json)
        raw = meta.get("subaccount_names")
        if isinstance(raw, list):
            names = [str(n).strip().lower() for n in raw if str(n).strip()]
        sub_names_by_account[a.id] = names

    type_candidates = (await ctx.db.execute(
        select(Transaction).where(
            Transaction.deleted_at.is_(None),
            Transaction.source == "pdf_import",
            Transaction.user_note.is_(None),
            Transaction.type.in_(("expense", "income")),
        )
    )).scalars().all()
    cross_cat_id = await _resolve_transfer_category(ctx.db, kind="cross_bank")
    sub_cat_id = await _resolve_transfer_category(ctx.db, kind="subaccount")
    # Audit stamp: any row promoted in this run gets a metadata trail so
    # the user can find / inspect / revert later. Without this, 116 rows
    # silently flip type and the user has no way to enumerate them.
    from datetime import datetime, timezone
    promoted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in type_candidates:
        token = _set_subaccount_names(sub_names_by_account.get(r.account_id, []))
        try:
            new_type, meta = _classify_transfer(
                r.raw_description or r.description or "", r.type,
            )
        finally:
            _reset_subaccount_names(token)
        if new_type != "transfer":
            continue
        original_type = r.type
        r.type = "transfer"
        promotion_meta: dict = {}
        if meta is not None and isinstance(meta, dict):
            promotion_meta.update(meta)
        promotion_meta["type_promoted_by"] = "refresh_matching"
        promotion_meta["type_promoted_at"] = promoted_at
        promotion_meta["type_promoted_from"] = original_type
        r.metadata_json = _merge_meta(r.metadata_json, promotion_meta)
        # Drop stale income/expense category (kind invariant)
        r.category_id = None
        if isinstance(meta, dict) and meta.get("subaccount"):
            if sub_cat_id is not None:
                r.category_id = sub_cat_id
        else:
            if cross_cat_id is not None:
                r.category_id = cross_cat_id
        r.is_pending = False
        ctx.summary["type_promoted_to_transfer"] += 1
        ctx.track_period(r.occurred_at)
    await ctx.db.flush()


# ─── Step 1: re-categorise uncategorised income/expense ─────────────────


async def step_recategorize(ctx: RefreshContext) -> None:
    rows = (await ctx.db.execute(
        select(Transaction).where(
            Transaction.deleted_at.is_(None),
            Transaction.category_id.is_(None),
            Transaction.type.in_(("expense", "income")),
            Transaction.source != "manual",
            Transaction.user_note.is_(None),
        )
    )).scalars().all()
    for r in rows:
        if await categorize_transaction(ctx.db, r):
            ctx.summary["recategorized"] += 1
            r.is_pending = False
            ctx.track_period(r.occurred_at)
    await ctx.db.flush()


# ─── Step 2: same-account ± pairs (subaccount detection) ────────────────


async def step_subaccount_pairs(ctx: RefreshContext) -> None:
    same = await detect_same_account_pairs(ctx.db)
    for out_tx, in_tx in same:
        await mark_subaccount_pair(ctx.db, out_tx, in_tx)
        ctx.summary["subaccount_pairs"] += 1
        ctx.track_period(out_tx.occurred_at)
        ctx.track_period(in_tx.occurred_at)
    await ctx.db.flush()


# ─── Step 3: single-leg IBAN ────────────────────────────────────────────


async def step_single_leg_iban(ctx: RefreshContext) -> None:
    single_leg = await detect_single_leg_iban(ctx.db)
    for tx_row in single_leg:
        ctx.summary["single_leg_iban"] += 1
        ctx.track_period(tx_row.occurred_at)
    await ctx.db.flush()


# ─── Step 4: cross-account auto-pair (≥75) ──────────────────────────────


async def step_auto_pair(ctx: RefreshContext) -> None:
    candidates = await find_transfer_pairs(ctx.db)
    used_ids: set[int] = set()
    for c in candidates:
        if c.score < SCORE_THRESHOLD_AUTO:
            continue
        if c.a.id in used_ids or c.b.id in used_ids:
            continue
        await pair_transactions(ctx.db, c.a, c.b)
        c.a.is_pending = False
        c.b.is_pending = False
        used_ids.add(c.a.id)
        used_ids.add(c.b.id)
        ctx.summary["auto_paired"] += 1
        ctx.track_period(c.a.occurred_at)
        ctx.track_period(c.b.occurred_at)
    await ctx.db.flush()


# ─── Step 5: orphan single-leg pairing ──────────────────────────────────


async def step_orphan_pair(ctx: RefreshContext) -> None:
    """`pair_orphan_single_legs` returns list[dict] with keys
    `orphan_id` / `counterpart_id`. The previous code unpacked it as a
    tuple and crashed on every refresh."""
    orphans = await pair_orphan_single_legs(ctx.db)
    ctx.summary["orphan_paired"] = len(orphans)
    for o in orphans:
        for tid in (o.get("orphan_id"), o.get("counterpart_id")):
            if tid is None:
                continue
            tx_row = (await ctx.db.execute(
                select(Transaction).where(Transaction.id == tid)
            )).scalar_one_or_none()
            if tx_row:
                ctx.track_period(tx_row.occurred_at)
    await ctx.db.flush()


# ─── Step 6: backfill 内部储蓄 for orphan subaccount transfers ────────


async def step_subaccount_category_backfill(ctx: RefreshContext) -> None:
    sub_cat_id = await _resolve_transfer_category(ctx.db, kind="subaccount")
    if sub_cat_id is None:
        return
    rows = (await ctx.db.execute(
        select(Transaction).where(
            Transaction.type == "transfer",
            Transaction.deleted_at.is_(None),
            Transaction.category_id.is_(None),
        )
    )).scalars().all()
    for r in rows:
        meta = _safe_meta(r.metadata_json)
        if meta.get("subaccount") is True:
            r.category_id = sub_cat_id
            ctx.summary["subaccount_orphans_categorized"] += 1
            ctx.track_period(r.occurred_at)
    await ctx.db.flush()


# ─── Step 7: re-enqueue uncategorised rows to inbox ─────────────────────


async def step_reenqueue_inbox(ctx: RefreshContext) -> None:
    rows = (await ctx.db.execute(
        select(Transaction).where(
            Transaction.deleted_at.is_(None),
            Transaction.category_id.is_(None),
            Transaction.is_pending.is_(False),
            Transaction.source != "manual",
            Transaction.user_note.is_(None),
            Transaction.type.in_(("expense", "income", "transfer")),
        )
    )).scalars().all()
    for r in rows:
        r.is_pending = True
        ctx.summary["reenqueued_to_inbox"] += 1
        ctx.track_period(r.occurred_at)
    await ctx.db.flush()


# ─── Step 8: recompute cashflow snapshots ───────────────────────────────


async def step_recompute_cashflow(ctx: RefreshContext) -> None:
    if ctx.affected_periods:
        await recompute_for_periods(ctx.db, ctx.affected_periods)
    ctx.summary["periods_recomputed"] = len(ctx.affected_periods)


# ─── Pipeline orchestrator ──────────────────────────────────────────────


PIPELINE: tuple[Callable, ...] = (
    step_clear_orphan_pointers,
    step_redetect_type,
    step_recategorize,
    step_subaccount_pairs,
    step_single_leg_iban,
    step_auto_pair,
    step_orphan_pair,
    step_subaccount_category_backfill,
    step_reenqueue_inbox,
    step_recompute_cashflow,
)


async def run_full_pipeline(ctx: RefreshContext) -> None:
    """Run every step in declared order. Each step flushes its own writes."""
    for step in PIPELINE:
        await step(ctx)
