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
    (and vice versa). +20 per direction match, capped at 30."""
    score = 0
    reasons: list[str] = []
    out_desc = (out_tx.description or "").lower()
    in_desc = (in_tx.description or "").lower()

    in_account = accounts.get(in_tx.account_id)
    out_account = accounts.get(out_tx.account_id)

    # Account NAME mention (e.g. "to Revolut", "from N26")
    if in_account and in_account.name and in_account.name.lower() in out_desc:
        score += 20; reasons.append(f"out→in name '{in_account.name}'")
    if out_account and out_account.name and out_account.name.lower() in in_desc:
        score += 10; reasons.append(f"in←out name '{out_account.name}'")

    # Owner-name self-transfer cue ("Jingsheng Chen" appears on both legs)
    self_pattern = re.compile(r"jingsheng\s+chen", re.I)
    if self_pattern.search(out_tx.description or "") and self_pattern.search(in_tx.description or ""):
        score += 10; reasons.append("self-transfer name match")

    # Generic transfer verbs already hint at it
    if any(k in out_desc for k in ("outgoing transfer", "to ", "payment to")):
        score += 5; reasons.append("outgoing-verb")
    if any(k in in_desc for k in ("incoming transfer", "payment from", "from ")):
        score += 5; reasons.append("incoming-verb")

    return min(score, 30), reasons


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
    """
    out_tx.type = "transfer"
    in_tx.type = "transfer"
    out_tx.counter_account_id = in_tx.account_id
    in_tx.counter_account_id = out_tx.account_id
    out_tx.metadata_json = _merge_meta(out_tx.metadata_json, {"transfer_direction": "out"})
    in_tx.metadata_json = _merge_meta(in_tx.metadata_json, {"transfer_direction": "in"})


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
    candidates = await find_transfer_pairs(db, candidate_ids=new_tx_ids)
    auto = [c for c in candidates if c.score >= SCORE_THRESHOLD_AUTO]
    suggested = [c for c in candidates if SCORE_THRESHOLD_SUGGEST <= c.score < SCORE_THRESHOLD_AUTO]

    paired_ids: list[tuple[int, int]] = []
    for c in auto:
        await pair_transactions(db, c.a, c.b)
        paired_ids.append((c.a.id, c.b.id))
        logger.info("transfer_paired", out_id=c.a.id, in_id=c.b.id, score=c.score, reasons=c.reasons)

    if paired_ids:
        await db.flush()

    return {
        "auto_paired": paired_ids,
        "suggested": [
            {"out_id": c.a.id, "in_id": c.b.id, "score": c.score, "reasons": c.reasons}
            for c in suggested
        ],
    }
