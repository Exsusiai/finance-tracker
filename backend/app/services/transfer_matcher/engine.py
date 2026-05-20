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

from app.models import Account, Category, Transaction

logger = structlog.get_logger(__name__)

WINDOW_DAYS = 5  # was 3, bumped 2026-05-09: AMEX charges can post up to 5 days
                 # after the bank-side debit (different settlement cycles).
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
    """Decay score by absolute date delta. Same day = 30 pts, drops to 0
    past day 5. Steep decay so same-day still strongly preferred over
    delayed bank settlement.
    """
    delta = abs((_date_only(d1) - _date_only(d2)).days)
    if delta == 0: return 30
    if delta == 1: return 20
    if delta == 2: return 10
    if delta == 3: return 5
    if delta == 4: return 3
    if delta == 5: return 2
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
    sub-account moves we definitively don't want to pair.

    Synthetic counter-legs (created by manual mark-transfer when the user
    couldn't find a real candidate) are also skipped — they're placeholders,
    not real money movements; pairing them with newly-imported real legs
    would leave 1 real + 1 synthetic + 1 new real = 3 rows for one transfer.
    """
    if tx.deleted_at is not None:
        return False
    if tx.type == "transfer" and tx.counter_account_id is not None:
        return False  # already paired
    if tx.metadata_json:
        try:
            meta = json.loads(tx.metadata_json)
            if isinstance(meta, dict):
                if meta.get("subaccount"):
                    return False  # sub-account moves stay unpaired
                if meta.get("synthetic_counterleg") is True:
                    return False  # synthetic mirror — should be replaced, not paired
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

    # Load all candidate transactions. When `candidate_ids` is given (post-
    # PDF-import path) we still scan globally — the counter-leg may live in
    # any account / period — but we narrow the date window down to ±90d
    # around the new batch so we don't pull millions of rows once history
    # builds up. The previous `or_(in_(ids), notin_(ids))` was logically
    # `TRUE`, which silenced the WHERE entirely.
    base = select(Transaction).where(Transaction.deleted_at.is_(None))
    if candidate_ids is not None:
        ids = list(candidate_ids)
        if not ids:
            return []
        # Bound the scan by date — pull dates of the candidate batch, then
        # widen by 4× window_days so we always have enough room for the
        # ±window_days score check without loading the whole table.
        from datetime import datetime, timedelta, timezone
        date_rows = (await db.execute(
            select(Transaction.occurred_at).where(Transaction.id.in_(ids))
        )).all()
        if date_rows:
            try:
                dts = [
                    datetime.fromisoformat(d[0].replace("Z", "+00:00"))
                    for d in date_rows if d[0]
                ]
                if dts:
                    delta = timedelta(days=window_days * 4)
                    lo = (min(dts) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
                    hi = (max(dts) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
                    base = base.where(
                        Transaction.occurred_at >= lo,
                        Transaction.occurred_at <= hi,
                    )
            except (ValueError, TypeError):
                pass  # fallback to unbounded scan if dates malformed
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
    # Add unpaired transfers. Direction priority:
    #   1. metadata.transfer_direction (set by parser / earlier user action)
    #   2. description hints (English / German "to/from/incoming/outgoing")
    #   3. fallback: drop into BOTH lists so the score-based pair search can
    #      still catch them. Without this fallback, AMEX-style statements
    #      (which write deposits as "Payment received - Thank You" with no
    #      directional verb) never matched the corresponding outflow leg.
    for r in rows:
        if r.type != "transfer" or r.counter_account_id is not None:
            continue
        meta_dir = None
        if r.metadata_json:
            try:
                m = json.loads(r.metadata_json)
                if isinstance(m, dict):
                    meta_dir = m.get("transfer_direction")
            except (json.JSONDecodeError, TypeError):
                pass
        if meta_dir == "out":
            outflows.append(r)
        elif meta_dir == "in":
            inflows.append(r)
        elif _looks_outflow(r):
            outflows.append(r)
        elif _looks_inflow(r):
            inflows.append(r)
        else:
            outflows.append(r)
            inflows.append(r)

    pairs: list[_Candidate] = []
    used_ids: set[int] = set()

    for out_tx in outflows:
        if out_tx.id in used_ids:
            continue
        for in_tx in inflows:
            if in_tx.id in used_ids:
                continue
            # Self-pair guard. The fallback in the directionless branch above
            # injects the same row into BOTH outflows and inflows, so without
            # this check `pair(r, r)` becomes a valid candidate (same amount,
            # same date → score 80, auto-confirmed) and we'd write a row's
            # paired_with_tx_id pointing at itself.
            if in_tx.id == out_tx.id:
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


async def replace_synthetic_with_real(
    db: AsyncSession,
    *,
    real_tx: Transaction,
    window_days: int = WINDOW_DAYS,
) -> Transaction | None:
    """When a newly-imported real tx matches an existing synthetic mirror in
    the same account, retire the mirror (soft-delete) and re-point the
    paired source row to the real one.

    The synthetic row was a placeholder the user manually created via
    mark-transfer; the real row supersedes it. Without this dedup, after the
    user imports the missing statement we'd have:
        source -500 (real, paired_with_tx_id → mirror)
        mirror +500 (synthetic, paired_with_tx_id → source)
        new +500 (real, unpaired)
    which is 3 rows for 1 transfer.

    Match criteria:
      same account_id
      same currency
      same amount (±0.01)
      occurred_at within ±window days
      mirror's metadata.synthetic_counterleg == true
      mirror not deleted
      real_tx still 'unpaired' (no counter_account_id)

    Returns the soft-deleted mirror row when an upgrade happened, else None.
    """
    if real_tx.deleted_at is not None or real_tx.counter_account_id is not None:
        return None

    from datetime import datetime, timedelta, timezone
    try:
        src_dt = datetime.fromisoformat(real_tx.occurred_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if src_dt.tzinfo is None:
        src_dt = src_dt.replace(tzinfo=timezone.utc)
    delta = timedelta(days=window_days)
    from_iso = (src_dt - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = (src_dt + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    real_amt = real_tx.amount if isinstance(real_tx.amount, Decimal) else Decimal(str(real_tx.amount))

    stmt = select(Transaction).where(
        Transaction.account_id == real_tx.account_id,
        Transaction.deleted_at.is_(None),
        Transaction.id != real_tx.id,
        Transaction.currency == real_tx.currency,
        Transaction.occurred_at >= from_iso,
        Transaction.occurred_at <= to_iso,
        Transaction.type == "transfer",
    )
    rows = (await db.execute(stmt)).scalars().all()

    for cand in rows:
        cand_amt = cand.amount if isinstance(cand.amount, Decimal) else Decimal(str(cand.amount))
        if abs(cand_amt - real_amt) >= Decimal("0.01"):
            continue
        meta: dict = {}
        if cand.metadata_json:
            try:
                meta = json.loads(cand.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        if not isinstance(meta, dict) or meta.get("synthetic_counterleg") is not True:
            continue

        # Found a synthetic mirror to replace. Re-point its source.
        source_id = meta.get("paired_with_tx_id")
        source_tx = None
        if source_id is not None:
            source_tx = (await db.execute(
                select(Transaction).where(
                    Transaction.id == source_id,
                    Transaction.deleted_at.is_(None),
                )
            )).scalar_one_or_none()

        if source_tx is not None:
            # Pair the source with the real row instead
            real_tx.type = "transfer"
            real_tx.counter_account_id = source_tx.account_id
            real_dir = meta.get("transfer_direction") or (
                "in" if cand.amount and Decimal(str(cand.amount)) > 0 else "out"
            )
            real_tx.metadata_json = _merge_meta(real_tx.metadata_json, {
                "transfer_direction": real_dir,
                "paired_with_tx_id": source_tx.id,
                "matched_by": "synthetic_replacement",
            })
            # Update source's pointer
            source_tx.counter_account_id = real_tx.account_id
            source_tx.metadata_json = _merge_meta(source_tx.metadata_json, {
                "paired_with_tx_id": real_tx.id,
            })
            # Inherit category from synthetic if real doesn't have one
            if real_tx.category_id is None and cand.category_id is not None:
                real_tx.category_id = cand.category_id

        # Soft-delete the synthetic; we don't want it counted in balances
        from datetime import datetime, timezone
        cand.deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "synthetic_replaced",
            real_id=real_tx.id,
            synthetic_id=cand.id,
            source_id=source_id,
        )
        return cand
    return None


async def list_counter_leg_candidates(
    db: AsyncSession,
    *,
    src_tx: Transaction,
    window_days: int = 10,
    amount_tolerance: Decimal | None = None,
) -> list[tuple[Transaction, str]]:
    """Return all tx in OTHER accounts that could plausibly be the other leg
    of `src_tx`. Each entry is `(candidate_tx, status)` where status is:

      - "free"               — unpaired; the typical case
      - "synthetic_bound"    — currently paired to a SYNTHETIC mirror leg.
                               The caller may re-bind by retiring the
                               synthetic and pairing to src_tx instead
                               (handled in the mark-transfer route).

    Filters: same currency, amount within ±`amount_tolerance`, occurred
    within ±`window_days`, NOT itself a synthetic counterleg, NOT a
    sub-account row, NOT already paired to a REAL counterpart.

    `amount_tolerance` defaults to 0.01 (cent precision). User-facing flows
    can pass a larger value when the two legs deliberately differ — e.g. a
    friend reimburses a meal but rounds the amount, or splits unevenly.

    Wider date window than `find_existing_counter_leg` (default 10 d vs 5 d)
    so edge cases like delayed credit-card settlements are visible. Caller
    is expected to render these as candidates and let the human disambiguate.
    """
    from datetime import datetime, timedelta, timezone

    try:
        src_dt = datetime.fromisoformat(src_tx.occurred_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return []
    if src_dt.tzinfo is None:
        src_dt = src_dt.replace(tzinfo=timezone.utc)
    delta = timedelta(days=window_days)
    from_iso = (src_dt - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = (src_dt + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    src_amt = src_tx.amount if isinstance(src_tx.amount, Decimal) else Decimal(str(src_tx.amount))
    tol = amount_tolerance if amount_tolerance is not None else Decimal("0.01")
    if tol < 0:
        tol = Decimal("0")

    # We DO NOT filter `counter_account_id IS NULL` at the SQL level — we
    # also want rows whose existing counterpart is a synthetic mirror, so
    # the user can re-bind them to a real reimbursement leg. The post-
    # filter loop classifies each row.
    stmt = select(Transaction).where(
        Transaction.account_id != src_tx.account_id,
        Transaction.deleted_at.is_(None),
        Transaction.id != src_tx.id,
        Transaction.currency == src_tx.currency,
        Transaction.occurred_at >= from_iso,
        Transaction.occurred_at <= to_iso,
    ).order_by(Transaction.occurred_at)
    rows = (await db.execute(stmt)).scalars().all()

    out: list[tuple[Transaction, str]] = []
    for c in rows:
        c_amt = c.amount if isinstance(c.amount, Decimal) else Decimal(str(c.amount))
        # Strict-equal special case: 0 tolerance means amounts must match exactly.
        # Otherwise use <= so the boundary value is included (tol=2 → ±2 inclusive).
        diff = abs(c_amt - src_amt)
        if tol == 0:
            if diff != 0:
                continue
        else:
            if diff > tol:
                continue
        meta: dict = {}
        if c.metadata_json:
            try:
                meta = json.loads(c.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        # Skip the candidate ITSELF being a synthetic mirror — pairing two
        # synthetics is meaningless.
        if meta.get("synthetic_counterleg") is True:
            continue
        if meta.get("subaccount") is True:
            continue

        paired_id = meta.get("paired_with_tx_id")
        if paired_id is None and c.counter_account_id is None:
            out.append((c, "free"))
            continue
        # Already paired. Check whether the partner is a synthetic mirror —
        # if so, this is a re-pair candidate (synthetic gets retired).
        if paired_id is not None:
            partner = (await db.execute(
                select(Transaction).where(Transaction.id == paired_id)
            )).scalar_one_or_none()
            if partner is None:
                continue  # orphan pointer
            try:
                partner_meta = (
                    json.loads(partner.metadata_json) if partner.metadata_json else {}
                ) or {}
            except (json.JSONDecodeError, TypeError):
                partner_meta = {}
            if not isinstance(partner_meta, dict):
                partner_meta = {}
            if partner_meta.get("synthetic_counterleg") is True:
                out.append((c, "synthetic_bound"))
                continue
        # Real-paired or otherwise: skip — re-binding would orphan the real
        # partner.
    return out


async def find_existing_counter_leg(
    db: AsyncSession,
    *,
    src_tx: Transaction,
    counter_account_id: int,
    window_days: int = WINDOW_DAYS,
) -> Transaction | None:
    """Look for an unpaired tx in `counter_account_id` that could be the
    other leg of `src_tx` — same amount, same currency, opposite-ish
    direction, within ±window days, NOT yet paired and NOT a synthetic
    counter-leg created by an earlier bind.

    Used by the manual-bind flow so that when both real legs already exist
    (e.g. user imported both N26 and AMEX statements but the matcher missed
    them), we attach them to each other instead of synthesising a duplicate.
    """
    from datetime import datetime, timedelta, timezone

    try:
        src_dt = datetime.fromisoformat(src_tx.occurred_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if src_dt.tzinfo is None:
        src_dt = src_dt.replace(tzinfo=timezone.utc)
    delta = timedelta(days=window_days)
    from_iso = (src_dt - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = (src_dt + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    src_amt = src_tx.amount if isinstance(src_tx.amount, Decimal) else Decimal(str(src_tx.amount))

    stmt = select(Transaction).where(
        Transaction.account_id == counter_account_id,
        Transaction.deleted_at.is_(None),
        Transaction.id != src_tx.id,
        Transaction.currency == src_tx.currency,
        Transaction.occurred_at >= from_iso,
        Transaction.occurred_at <= to_iso,
        Transaction.counter_account_id.is_(None),
    )
    rows = (await db.execute(stmt)).scalars().all()

    for c in rows:
        c_amt = c.amount if isinstance(c.amount, Decimal) else Decimal(str(c.amount))
        if abs(c_amt - src_amt) >= Decimal("0.01"):
            continue
        meta: dict = {}
        if c.metadata_json:
            try:
                meta = json.loads(c.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("paired_with_tx_id") is not None:
            continue
        if meta.get("synthetic_counterleg") is True:
            continue  # don't bind two synthetic mirrors to each other
        if meta.get("subaccount") is True:
            continue
        return c
    return None


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
    # Symmetric pointers — without `paired_with_tx_id` on both sides,
    # the orphan-pointer cleanup in refresh-matching's Step -1 sees a row
    # with counter_account_id set but no metadata back-pointer, treats it
    # as orphan, clears counter_account_id, and the candidates resurface
    # in the suggestions panel after every refresh. mark_subaccount_pair
    # already wrote this; pair_transactions historically didn't.
    out_tx.metadata_json = _merge_meta(out_tx.metadata_json, {
        "transfer_direction": "out",
        "paired_with_tx_id": in_tx.id,
    })
    in_tx.metadata_json = _merge_meta(in_tx.metadata_json, {
        "transfer_direction": "in",
        "paired_with_tx_id": out_tx.id,
    })

    # Resolve auto-category once based on the counter account's type
    # (e.g. credit_card → 信用卡还款, bank → 跨行划转).
    out_acct = (await db.execute(
        _select(Account).where(Account.id == out_tx.account_id)
    )).scalar_one_or_none()
    in_acct = (await db.execute(
        _select(Account).where(Account.id == in_tx.account_id)
    )).scalar_one_or_none()
    out_default = await _resolve_transfer_category(db, kind="auto", counter_account=in_acct)
    in_default = await _resolve_transfer_category(db, kind="auto", counter_account=out_acct)

    for leg, default_cat in ((out_tx, out_default), (in_tx, in_default)):
        if leg.category_id is None:
            # Bug 1: previously left None → row appeared as "未分类" in
            # the breakdown view. Now auto-pick a transfer-kind category.
            leg.category_id = default_cat
            continue
        cat = (await db.execute(
            _select(Category).where(Category.id == leg.category_id)
        )).scalar_one_or_none()
        if cat is None or cat.kind == "transfer":
            continue  # already valid
        # Try a transfer-kind category with the same name (preserve user's
        # learned classification when possible).
        replacement = (await db.execute(
            _select(Category).where(
                Category.kind == "transfer", Category.name == cat.name
            )
        )).scalar_one_or_none()
        leg.category_id = replacement.id if replacement is not None else default_cat

    # V6-P1-1: clear is_pending on both legs so auto-paired transfers don't
    # stay visible in the inbox forever. Idempotent — already-False is fine.
    out_tx.is_pending = False
    in_tx.is_pending = False
    import app.models as _models
    _models.touch_updated_at(out_tx)
    _models.touch_updated_at(in_tx)


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

    # Step 3 (2026-05-06): single-leg IBAN detection. When a tx's
    # description / raw_description contains an IBAN that belongs to one
    # of the user's OWN accounts (i.e. they linked it in Settings), it's
    # an internal transfer even if the counterpart PDF hasn't been
    # uploaded yet. The previous logic required both legs to exist for
    # cross-account pairing — leaving the user to hand-confirm these.
    single_leg_paired = await detect_single_leg_iban(db, candidate_ids=new_tx_ids)

    # Step 4 (2026-05-06, Bug 2): pair orphan single-leg transfers with
    # the now-uploaded counter-account income/expense rows. After PDF A
    # is processed and one row is promoted to single-leg transfer (Step
    # 3), the counterpart income on PDF B doesn't reach the regular
    # cross-account matcher because the orphan's counter_account_id is
    # already set (so _is_eligible skips it). Walk every orphan after
    # the new batch lands and look for its counterpart.
    orphan_paired = await pair_orphan_single_legs(db)

    if paired_ids or same_paired_ids or single_leg_paired or orphan_paired:
        await db.flush()

    return {
        "auto_paired": paired_ids,
        "subaccount_paired": same_paired_ids,
        "single_leg_iban": single_leg_paired,
        "orphan_paired": orphan_paired,
        "suggested": [
            {"out_id": c.a.id, "in_id": c.b.id, "score": c.score, "reasons": c.reasons}
            for c in suggested
        ],
    }


async def pair_orphan_single_legs(db: AsyncSession) -> list[dict]:
    """For every transfer row that was promoted to a single-leg transfer
    via IBAN match but whose counter-leg was not yet in the DB, look
    again — the counterpart PDF may have arrived since.

    Match criteria:
      orphan.counter_account_id == candidate.account_id
      candidate is income/expense (not yet a transfer)
      |orphan.amount| ≈ |candidate.amount|  (currency must match)
      occurred_at within ±WINDOW_DAYS

    On hit:
      orphan: metadata.paired_with_tx_id = candidate.id
              (matched_by stays so we know how it found its partner)
      candidate: type='transfer'
                 counter_account_id = orphan.account_id
                 transfer_direction = inverse of orphan's
                 metadata.paired_with_tx_id = orphan.id
                 metadata.matched_by = 'iban_orphan_pair'
                 is_pending = False
                 category remapped to transfer-kind
    """
    orphans = (await db.execute(
        select(Transaction).where(
            Transaction.deleted_at.is_(None),
            Transaction.type == "transfer",
            Transaction.counter_account_id.is_not(None),
        )
    )).scalars().all()

    paired: list[dict] = []
    for orphan in orphans:
        # Need to re-parse metadata each iteration.
        try:
            meta = json.loads(orphan.metadata_json) if orphan.metadata_json else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            continue
        if meta.get("paired_with_tx_id"):
            continue  # already paired with a real counterpart
        if meta.get("subaccount"):
            continue  # in-bank, no cross-account counterpart needed
        # Look for the counterpart in the orphan's counter_account.
        try:
            orphan_date = _date_only(orphan.occurred_at)
        except (ValueError, TypeError):
            continue
        amount = abs(orphan.amount) if orphan.amount is not None else None
        if amount is None or amount == 0:
            continue
        currency = orphan.currency
        # opposite leg's required type
        orphan_dir = meta.get("transfer_direction")
        if orphan_dir == "out":
            wanted_type = "income"
        elif orphan_dir == "in":
            wanted_type = "expense"
        else:
            continue

        candidates = (await db.execute(
            select(Transaction).where(
                Transaction.deleted_at.is_(None),
                Transaction.account_id == orphan.counter_account_id,
                Transaction.type == wanted_type,
                Transaction.currency == currency,
                Transaction.amount == amount,
            )
        )).scalars().all()
        # Pick the closest by date within window
        best = None
        for cand in candidates:
            try:
                days = abs((_date_only(cand.occurred_at) - orphan_date).days)
            except (ValueError, TypeError):
                continue
            if days > WINDOW_DAYS:
                continue
            if best is None or days < best[1]:
                best = (cand, days)
        if best is None:
            continue
        cand_tx = best[0]

        # Promote candidate to transfer + cross-link both sides.
        cand_dir = "in" if orphan_dir == "out" else "out"
        cand_tx.type = "transfer"
        cand_tx.counter_account_id = orphan.account_id
        cand_tx.is_pending = False
        cand_tx.metadata_json = _merge_meta(cand_tx.metadata_json, {
            "transfer_direction": cand_dir,
            "matched_by": "iban_orphan_pair",
            "paired_with_tx_id": orphan.id,
        })
        # Update orphan to record the new counterpart.
        orphan.metadata_json = _merge_meta(orphan.metadata_json, {
            "paired_with_tx_id": cand_tx.id,
        })

        # Remap candidate's category to transfer kind (Bug 1: don't leave
        # 未分类). Use the orphan's account as counter to choose subcat.
        own_account = (await db.execute(
            select(Account).where(Account.id == orphan.account_id)
        )).scalar_one_or_none()
        default_cat_id = await _resolve_transfer_category(
            db, kind="auto", counter_account=own_account,
        )
        if cand_tx.category_id is None:
            cand_tx.category_id = default_cat_id
        else:
            cat = (await db.execute(
                select(Category).where(Category.id == cand_tx.category_id)
            )).scalar_one_or_none()
            if cat is not None and cat.kind != "transfer":
                replacement = (await db.execute(
                    select(Category).where(
                        Category.kind == "transfer", Category.name == cat.name
                    )
                )).scalar_one_or_none()
                cand_tx.category_id = replacement.id if replacement is not None else default_cat_id

        paired.append({
            "orphan_id": orphan.id,
            "counterpart_id": cand_tx.id,
            "direction_orphan": orphan_dir,
            "direction_counterpart": cand_dir,
        })
        logger.info(
            "iban_orphan_paired",
            orphan_id=orphan.id,
            counterpart_id=cand_tx.id,
            orphan_dir=orphan_dir,
        )
    return paired


async def detect_single_leg_iban(
    db: AsyncSession,
    *,
    candidate_ids: Iterable[int] | None = None,
) -> list[dict]:
    """Identify single-leg internal transfers via IBAN match.

    For each not-yet-paired tx (type expense/income, not subaccount), check
    whether its description / raw_description contains an IBAN belonging
    to one of the user's OTHER active accounts. If so, promote the row to
    type='transfer', set counter_account_id, and tag transfer_direction
    (out for expense, in for income). The user can later upload the
    counterpart PDF and the matcher will recognise both sides are already
    transfers (idempotent).
    """
    base = (
        select(Transaction)
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.type.in_(("expense", "income")),
            Transaction.counter_account_id.is_(None),
        )
    )
    if candidate_ids is not None:
        ids = list(candidate_ids)
        if not ids:
            return []
        base = base.where(Transaction.id.in_(ids))
    rows = (await db.execute(base)).scalars().all()
    if not rows:
        return []

    # Index every other account's IBAN (uppercased, no spaces).
    accounts = (await db.execute(
        select(Account).where(Account.deleted_at.is_(None))
    )).scalars().all()
    iban_to_account: dict[str, Account] = {}
    for a in accounts:
        if not a.iban:
            continue
        norm = a.iban.upper().replace(" ", "")
        if len(norm) >= 8:
            iban_to_account[norm] = a
    if not iban_to_account:
        return []

    matched: list[dict] = []
    for tx in rows:
        # Skip rows already flagged as sub-account moves.
        if tx.metadata_json:
            try:
                m = json.loads(tx.metadata_json)
                if isinstance(m, dict) and m.get("subaccount"):
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
        haystack = (
            (tx.description or "") + " " + (tx.raw_description or "")
        ).upper().replace(" ", "")
        # Find the FIRST own-account IBAN appearing in the description that
        # isn't tx's own account (avoid self-pair on N26 footer-leak rows).
        hit_account: Account | None = None
        for iban, acct in iban_to_account.items():
            if acct.id == tx.account_id:
                continue
            if iban in haystack:
                hit_account = acct
                break
        if hit_account is None:
            continue

        direction = "out" if tx.type == "expense" else "in"
        original_type = tx.type
        tx.type = "transfer"
        tx.counter_account_id = hit_account.id
        tx.is_pending = False
        tx.metadata_json = _merge_meta(tx.metadata_json, {
            "transfer_direction": direction,
            "matched_by": "iban_single_leg",
            "matched_iban": hit_account.iban,
        })
        # Bug 1 fix: auto-pick a transfer-kind sub-category based on the
        # counter account's type so the row is not stuck as "未分类".
        default_cat_id = await _resolve_transfer_category(
            db, kind="auto", counter_account=hit_account,
        )
        if tx.category_id is None:
            tx.category_id = default_cat_id
        else:
            cat = (await db.execute(
                select(Category).where(Category.id == tx.category_id)
            )).scalar_one_or_none()
            if cat is not None and cat.kind != "transfer":
                # Try preserving the user-learned name first, fall back to
                # auto-resolved.
                replacement = (await db.execute(
                    select(Category).where(
                        Category.kind == "transfer", Category.name == cat.name
                    )
                )).scalar_one_or_none()
                tx.category_id = replacement.id if replacement is not None else default_cat_id
        matched.append({
            "tx_id": tx.id,
            "counter_account_id": hit_account.id,
            "counter_account_name": hit_account.name,
            "direction": direction,
            "iban": hit_account.iban,
            "from_type": original_type,
        })
        logger.info(
            "single_leg_iban_matched",
            tx_id=tx.id,
            counter_account=hit_account.name,
            direction=direction,
        )
    return matched


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

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[\"'`'']", "", s.lower())).strip()

    na, nb = _norm(a), _norm(b)
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
    # 2026-05-06 (Bug 1): subaccount transfers should be auto-categorised so
    # the user doesn't see "未分类" in the breakdown view.
    cat_id = await _resolve_transfer_category(db, kind="subaccount")
    if cat_id is not None:
        if out_tx.category_id is None:
            out_tx.category_id = cat_id
        if in_tx.category_id is None:
            in_tx.category_id = cat_id

    # V6-P1-1: clear is_pending on both legs. Idempotent — already-False is fine.
    out_tx.is_pending = False
    in_tx.is_pending = False
    import app.models as _models
    _models.touch_updated_at(out_tx)
    _models.touch_updated_at(in_tx)


async def _resolve_transfer_category(
    db: AsyncSession,
    *,
    kind: str,
    counter_account: "Account | None" = None,  # noqa: ARG001 - reserved for future
) -> int | None:
    """Pick a transfer-kind sub-category by intent.

    Simplified per user spec (2026-05-06): we no longer try to differentiate
    "credit_card_payback" / "investing" / "cross_bank" — auto-categorising
    bank↔credit_card transfers as 信用卡还款 turned out to be brittle (the
    bank-side leg and credit-card-side leg ended up in different categories,
    confusing the user). The only special case worth keeping is the in-bank
    sub-account move, which we tag as 内部储蓄 because it's invisible noise
    from the user's whole-portfolio perspective.

      `kind="subaccount"`  → 内部储蓄
      everything else      → 跨行划转

    The `counter_account` parameter is kept in the signature for callers
    that already pass it but is no longer consulted; if we re-introduce
    the heuristic later it doesn't require a callsite change.
    """
    target_name = "内部储蓄" if kind == "subaccount" else "跨行划转"
    row = (await db.execute(
        select(Category).where(
            Category.kind == "transfer", Category.name == target_name
        )
    )).scalar_one_or_none()
    return row.id if row else None
