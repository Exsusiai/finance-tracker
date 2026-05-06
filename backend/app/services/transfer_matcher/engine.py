"""Cross-account transfer matcher.

Scans recently-written transactions and tries to pair an `expense` row in one
account with a corresponding `income` row in another account. When confidence
is high enough, both rows are promoted to `type='transfer'` and their
`counter_account_id` cross-link.

Scoring (max 100):
  - Amount equal:            50  (must match; sub-50 candidates are dropped)
  - Date proximity:          0..30  (same day=30, ±1d=20, ±2d=10, ±3d=5)
  - Description hints:       0..30  (counterparty bank/IBAN keywords found)

Default thresholds (configurable):
  - >= AUTO (75): pair automatically
  - 50..AUTO   : suggest but leave pending for user confirmation (future UI)
  - < 50       : ignore
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

import structlog
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Transaction

logger = structlog.get_logger(__name__)

WINDOW_DAYS = 3
SCORE_THRESHOLD_AUTO = 75
SCORE_THRESHOLD_SUGGEST = 50


# ─── Scoring ────────────────────────────────────────────────────────────


@dataclass
class _Candidate:
    a: Transaction          # the outgoing leg (expense)
    b: Transaction          # the incoming leg (income)
    score: int
    reasons: list[str]


def _date_only(dt_str: str) -> datetime:
    """`occurred_at` may be 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DD'."""
    return datetime.strptime(dt_str[:10], "%Y-%m-%d")


def _date_score(d1: str, d2: str) -> int:
    delta = abs((_date_only(d1) - _date_only(d2)).days)
    if delta == 0: return 30
    if delta == 1: return 20
    if delta == 2: return 10
    if delta == 3: return 5
    return 0


def _hint_score(out_tx: Transaction, in_tx: Transaction,
                accounts: dict[int, Account]) -> tuple[int, list[str]]:
    """Look for evidence that `out_tx` describes a transfer to `in_tx`'s account
    (and vice versa). Capped at 50 (IBAN match alone can hit 40, putting amount-
    matched + IBAN at 90 which clears the auto-pair threshold)."""
    score = 0
    reasons: list[str] = []
    # Combine description + raw_description: PDF parsers may stash IBAN /
    # counterparty IBAN on continuation lines into raw_description.
    out_desc_raw = ((out_tx.description or "") + " " + (out_tx.raw_description or "")).strip()
    in_desc_raw = ((in_tx.description or "") + " " + (in_tx.raw_description or "")).strip()
    out_desc = out_desc_raw.lower()
    in_desc = in_desc_raw.lower()

    in_account = accounts.get(in_tx.account_id)
    out_account = accounts.get(out_tx.account_id)

    # ── HIGHEST CONFIDENCE: IBAN match (own bank's IBAN appears in counter-leg description)
    # When a self-transfer prints the same owner name on both legs, names give
    # us nothing — but the destination IBAN is unique to one bank.
    out_iban = (out_account.iban or "").upper().replace(" ", "") if out_account else ""
    in_iban = (in_account.iban or "").upper().replace(" ", "") if in_account else ""
    out_desc_norm = out_desc_raw.upper().replace(" ", "")
    in_desc_norm = in_desc_raw.upper().replace(" ", "")
    # in_tx's account IBAN should appear in out_tx's description (we're sending TO it)
    if in_iban and len(in_iban) >= 8 and in_iban in out_desc_norm:
        score += 40; reasons.append(f"out→in IBAN match ({in_iban[:6]}…)")
    # …or vice versa
    if out_iban and len(out_iban) >= 8 and out_iban in in_desc_norm:
        score += 20; reasons.append(f"in←out IBAN match ({out_iban[:6]}…)")

    # Account NAME mention (e.g. "to Revolut", "from N26")
    if in_account and in_account.name and in_account.name.lower() in out_desc:
        score += 20; reasons.append(f"out→in name '{in_account.name}'")
    if out_account and out_account.name and out_account.name.lower() in in_desc:
        score += 10; reasons.append(f"in←out name '{out_account.name}'")

    # Owner-name self-transfer cue (configured via FINANCE_TRACKER_OWNER_NAMES;
    # e.g. when both legs print the same account-holder name).
    from app.core.config import get_settings

    for owner in get_settings().owner_names:
        if not owner:
            continue
        pattern = re.compile(re.escape(owner), re.I)
        if pattern.search(out_desc_raw) and pattern.search(in_desc_raw):
            score += 10
            reasons.append("self-transfer name match")
            break

    # Generic transfer verbs already hint at it
    if any(k in out_desc for k in ("outgoing transfer", "to ", "payment to")):
        score += 5; reasons.append("outgoing-verb")
    if any(k in in_desc for k in ("incoming transfer", "payment from", "from ")):
        score += 5; reasons.append("incoming-verb")

    return min(score, 50), reasons


def _is_eligible(tx: Transaction) -> bool:
    """Skip rows that are already paired, already typed transfer, deleted, or
    sub-account moves we definitively don't want to pair."""
    if tx.deleted_at is not None:
        return False
    if tx.type == "transfer" and tx.counter_account_id is not None:
        return False  # already paired
    if tx.metadata_json:
        try:
            meta = json.loads(tx.metadata_json)
            if isinstance(meta, dict) and meta.get("subaccount"):
                return False  # sub-account moves stay unpaired
        except (json.JSONDecodeError, TypeError):
            pass
    return True


# ─── Public API ─────────────────────────────────────────────────────────


async def find_transfer_pairs(
    db: AsyncSession,
    *,
    candidate_ids: Iterable[int] | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[_Candidate]:
    """Scan transactions for transfer pair candidates.

    If `candidate_ids` is given, only pair those rows against the rest of the
    table (used after a PDF batch import). Otherwise scan everything.
    """
    # Load accounts once
    acc_rows = (await db.execute(select(Account))).scalars().all()
    accounts = {a.id: a for a in acc_rows}

    # Load all candidate transactions
    base = select(Transaction).where(Transaction.deleted_at.is_(None))
    if candidate_ids is not None:
        ids = list(candidate_ids)
        if not ids:
            return []
        # Pull both the new batch AND any other tx within ±window days that
        # could be the counter-leg (we don't know dates yet — let SQL do all,
        # then filter in Python by window).
        base = base.where(or_(Transaction.id.in_(ids), Transaction.id.notin_(ids)))
    rows = (await db.execute(base)).scalars().all()
    rows = [r for r in rows if _is_eligible(r)]

    # Group candidates into outflows / inflows. Includes:
    #   - vanilla expense / income rows
    #   - already-typed transfers without a counter_account_id (PDF parser may
    #     have pre-tagged them via cross-bank-hint keywords; matcher's job is
    #     to pair them up across accounts)
    _OUT_HINTS = ("to ", "outgoing", "payment to", "transfer to", "sepa direct")
    _IN_HINTS  = ("from ", "incoming", "payment from", "transfer from", "deposit")

    def _looks_outflow(t: Transaction) -> bool:
        d = (t.description or "").lower()
        return any(h in d for h in _OUT_HINTS)
    def _looks_inflow(t: Transaction) -> bool:
        d = (t.description or "").lower()
        return any(h in d for h in _IN_HINTS)

    outflows = [r for r in rows if r.type == "expense"]
    inflows  = [r for r in rows if r.type == "income"]
    # Add unpaired transfers based on description direction
    for r in rows:
        if r.type == "transfer" and r.counter_account_id is None:
            if _looks_outflow(r):
                outflows.append(r)
            elif _looks_inflow(r):
                inflows.append(r)

    pairs: list[_Candidate] = []
    used_ids: set[int] = set()

    for out_tx in outflows:
        if out_tx.id in used_ids:
            continue
        for in_tx in inflows:
            if in_tx.id in used_ids:
                continue
            if in_tx.account_id == out_tx.account_id:
                continue  # same account — not cross-bank
            # Amount must match
            if Decimal(str(out_tx.amount)) != Decimal(str(in_tx.amount)):
                continue
            # Currency must match (or one is base — we keep it strict for v1)
            if out_tx.currency != in_tx.currency:
                continue
            # Date window
            try:
                if abs((_date_only(out_tx.occurred_at) - _date_only(in_tx.occurred_at)).days) > window_days:
                    continue
            except (ValueError, TypeError):
                continue

            score = 50  # amount-equal baseline
            reasons = ["amount-equal"]
            score += _date_score(out_tx.occurred_at, in_tx.occurred_at)
            hint, hint_reasons = _hint_score(out_tx, in_tx, accounts)
            score += hint
            reasons += hint_reasons

            if score >= SCORE_THRESHOLD_SUGGEST:
                pairs.append(_Candidate(a=out_tx, b=in_tx, score=score, reasons=reasons))
                if score >= SCORE_THRESHOLD_AUTO:
                    used_ids.add(out_tx.id)
                    used_ids.add(in_tx.id)
                    break  # this outflow is matched, move on

    # Sort: high-score first (caller may pair top-down)
    pairs.sort(key=lambda c: -c.score)
    return pairs


async def pair_transactions(
    db: AsyncSession,
    out_tx: Transaction,
    in_tx: Transaction,
) -> None:
    """Mark `out_tx` and `in_tx` as a confirmed transfer pair (idempotent).

    Crucially we tag each leg's `metadata_json.transfer_direction` so the
    balance view can apply the correct sign — without this the view's
    type='transfer' branch (default `-ABS`) would deduct from BOTH accounts.

    2026-05-06 fix: when the row was previously auto-categorised by an
    income/expense rule (e.g. "TF Bank AB" → expense:信用卡还款) and is
    now being promoted to a transfer, the old category_id points at a
    category whose kind != 'transfer' — that violates the FIX-5 kind
    invariant. Try to remap to a transfer-kind category with the same
    name (so 'expense:信用卡还款' becomes 'transfer:信用卡还款' if it
    exists), otherwise drop the category_id so the user can re-pick.
    """
    from sqlalchemy import select as _select

    from app.models import Category

    out_tx.type = "transfer"
    in_tx.type = "transfer"
    out_tx.counter_account_id = in_tx.account_id
    in_tx.counter_account_id = out_tx.account_id
    out_tx.metadata_json = _merge_meta(out_tx.metadata_json, {"transfer_direction": "out"})
    in_tx.metadata_json = _merge_meta(in_tx.metadata_json, {"transfer_direction": "in"})

    for leg in (out_tx, in_tx):
        if leg.category_id is None:
            continue
        cat = (await db.execute(
            _select(Category).where(Category.id == leg.category_id)
        )).scalar_one_or_none()
        if cat is None or cat.kind == "transfer":
            continue  # already valid (or category gone)
        # Try a transfer-kind category with the same name.
        replacement = (await db.execute(
            _select(Category).where(
                Category.kind == "transfer", Category.name == cat.name
            )
        )).scalar_one_or_none()
        leg.category_id = replacement.id if replacement is not None else None


def _merge_meta(existing: str | None, new: dict) -> str:
    """Shallow-merge a dict into an existing JSON string (or create a fresh one)."""
    if existing:
        try:
            cur = json.loads(existing)
            if not isinstance(cur, dict):
                cur = {}
        except (json.JSONDecodeError, TypeError):
            cur = {}
    else:
        cur = {}
    cur.update(new)
    return json.dumps(cur)


async def auto_pair_after_import(
    db: AsyncSession,
    new_tx_ids: Iterable[int],
) -> dict:
    """Run the matcher on a freshly imported batch; auto-pair high-confidence
    matches. Returns a summary dict for caller logging."""
    # Step 1: same-account amount pairing → flag as in-bank sub-account moves
    same_acct = await detect_same_account_pairs(db, candidate_ids=new_tx_ids)
    same_paired_ids: list[tuple[int, int]] = []
    for out_tx, in_tx in same_acct:
        await mark_subaccount_pair(db, out_tx, in_tx)
        same_paired_ids.append((out_tx.id, in_tx.id))
        logger.info("subaccount_pair_marked", out_id=out_tx.id, in_id=in_tx.id)

    # Step 2: cross-account transfer matcher
    candidates = await find_transfer_pairs(db, candidate_ids=new_tx_ids)
    auto = [c for c in candidates if c.score >= SCORE_THRESHOLD_AUTO]
    suggested = [c for c in candidates if SCORE_THRESHOLD_SUGGEST <= c.score < SCORE_THRESHOLD_AUTO]

    paired_ids: list[tuple[int, int]] = []
    for c in auto:
        # Skip if either side was already marked as sub-account in step 1
        if c.a.type == "transfer" and json.loads(c.a.metadata_json or "{}").get("subaccount"):
            continue
        if c.b.type == "transfer" and json.loads(c.b.metadata_json or "{}").get("subaccount"):
            continue
        await pair_transactions(db, c.a, c.b)
        paired_ids.append((c.a.id, c.b.id))
        logger.info("transfer_paired", out_id=c.a.id, in_id=c.b.id, score=c.score, reasons=c.reasons)

    if paired_ids or same_paired_ids:
        await db.flush()

    return {
        "auto_paired": paired_ids,
        "subaccount_paired": same_paired_ids,
        "suggested": [
            {"out_id": c.a.id, "in_id": c.b.id, "score": c.score, "reasons": c.reasons}
            for c in suggested
        ],
    }


# ─── Same-account amount-matching heuristic ────────────────────────────


async def detect_same_account_pairs(
    db: AsyncSession,
    *,
    candidate_ids: Iterable[int] | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[tuple[Transaction, Transaction]]:
    """Within the SAME account, find +X / -X pairs within `window_days`.

    Heuristic: when the same statement has both an outgoing (expense) and an
    incoming (income) row of identical amount + currency within ±N days, it's
    almost always an in-bank sub-account move (e.g. main → "Investing" Space).
    Returns pairs `(out_tx, in_tx)` to be marked as `subaccount=true`.

    This catches user-customised sub-account names that the keyword list and
    user list both miss (e.g. user added a Space but didn't add it to the
    settings list yet).
    """
    base = select(Transaction).where(Transaction.deleted_at.is_(None))
    rows = (await db.execute(base)).scalars().all()
    rows = [r for r in rows if _is_eligible(r)]

    cand_set = set(candidate_ids) if candidate_ids is not None else None

    # Bucket by (account_id, amount, currency)
    buckets: dict[tuple[int, str, str], list[Transaction]] = {}
    for r in rows:
        if r.type not in ("expense", "income"):
            continue
        key = (r.account_id, str(r.amount), r.currency)
        buckets.setdefault(key, []).append(r)

    pairs: list[tuple[Transaction, Transaction]] = []
    used: set[int] = set()

    for (_acct, _amt, _cur), bucket in buckets.items():
        outs = [r for r in bucket if r.type == "expense" and r.id not in used]
        ins  = [r for r in bucket if r.type == "income" and r.id not in used]
        # Sort each side by date so we pair the closest legitimate counterpart
        # rather than the first one we happen to iterate over.
        for out_tx in outs:
            best: tuple[Transaction, int] | None = None  # (in_tx, days_diff)
            for in_tx in ins:
                if in_tx.id in used:
                    continue
                if cand_set is not None and out_tx.id not in cand_set and in_tx.id not in cand_set:
                    continue
                try:
                    days = abs((_date_only(out_tx.occurred_at) - _date_only(in_tx.occurred_at)).days)
                except (ValueError, TypeError):
                    continue
                if days > window_days:
                    continue
                # CRITICAL: a same-account ±X pair only counts as an internal
                # sub-account move if the two sides describe the *same* event.
                # Without this guard, a "To Saving -500" and an unrelated
                # "Apple Pay deposit +500" on the same day would be falsely
                # paired just because their amounts match.
                if not _descriptions_match(out_tx.description, in_tx.description):
                    continue
                if best is None or days < best[1]:
                    best = (in_tx, days)
            if best is not None:
                pairs.append((out_tx, best[0]))
                used.add(out_tx.id)
                used.add(best[0].id)

    return pairs


def _descriptions_match(a: str | None, b: str | None) -> bool:
    """True when two same-account ± rows look like halves of the same event.

    We accept either:
      - exact match (case-insensitive, after stripping quotes/extra whitespace)
      - sharing a meaningful token (≥4 chars, not in noise list)
    """
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"\s+", " ", re.sub(r"[\"'`'']", "", s.lower())).strip()
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    # Token overlap fallback for when one side has more context
    noise = {"the", "from", "to", "by", "for", "and", "into", "of", "via", "card", "fee"}
    tokens_a = {t for t in re.split(r"[\s\-_/*\":]", na) if len(t) >= 4 and t not in noise}
    tokens_b = {t for t in re.split(r"[\s\-_/*\":]", nb) if len(t) >= 4 and t not in noise}
    return bool(tokens_a & tokens_b)


async def mark_subaccount_pair(
    db: AsyncSession,
    out_tx: Transaction,
    in_tx: Transaction,
) -> None:
    """Tag a same-account ± pair as in-bank sub-account moves.

    Both legs become `type='transfer'` with `metadata.subaccount=true`. The
    balance view skips them (money stayed inside the bank); cash-flow doesn't
    count them as income/expense.
    """
    out_tx.type = "transfer"
    in_tx.type = "transfer"
    out_tx.metadata_json = _merge_meta(
        out_tx.metadata_json,
        {"subaccount": True, "matched": "amount_match_heuristic", "source": "same_account_pair",
         "transfer_direction": "out", "paired_with_tx_id": in_tx.id},
    )
    in_tx.metadata_json = _merge_meta(
        in_tx.metadata_json,
        {"subaccount": True, "matched": "amount_match_heuristic", "source": "same_account_pair",
         "transfer_direction": "in", "paired_with_tx_id": out_tx.id},
    )
