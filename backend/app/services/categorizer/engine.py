"""Categorization engine — rule-based + auto-learning.

Forward direction (`categorize_transaction`):
    Run all enabled rules against a transaction. Highest-priority match wins;
    returns True if a category was set.

Reverse direction (`learn_from_user_assignment`):
    When a user manually picks/changes a transaction's category, derive a new
    rule from the transaction text so future similar transactions auto-match.
    De-duplicates against existing rules (bumps priority instead of inserting).
"""

from __future__ import annotations

import concurrent.futures
import re

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.models import CategorizationRule, Category, Transaction

logger = structlog.get_logger(__name__)

# Maximum allowed regex pattern length (chars)
_MAX_REGEX_LEN = 200
# Heuristic pattern: nested quantifiers like (a+)+, (.*)+, (a|b)*  etc.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]{0,40}[+*]\)[+*?]")
# Reusable single-thread pool for regex timeout isolation
_regex_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="regex_guard")


def validate_regex_complexity(pattern: str) -> None:
    """Validate a regex pattern for safety before storing it.

    Raises InvalidInputError when the pattern is too long, has catastrophic
    backtracking shapes, or fails to compile.
    """
    if len(pattern) > _MAX_REGEX_LEN:
        raise InvalidInputError(
            f"Regex pattern too long ({len(pattern)} chars); maximum is {_MAX_REGEX_LEN}"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise InvalidInputError(
            "Regex pattern has nested quantifiers; would cause catastrophic backtracking"
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise InvalidInputError(f"Invalid regex: {exc}") from exc


def _safe_regex_search(pattern: str, value: str, *, timeout_sec: float = 1.0) -> bool:
    """Run re.search inside a thread with a wall-clock timeout.

    Returns False (no match) if the pattern times out or is invalid,
    so a misbehaving rule degrades gracefully instead of hanging the process.
    """
    try:
        future = _regex_executor.submit(re.search, pattern, value, re.IGNORECASE)
        result = future.result(timeout=timeout_sec)
        return bool(result)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "regex_timeout",
            pattern=pattern[:80],
            value_len=len(value),
        )
        return False
    except re.error:
        return False


# ─── Forward: rule-based categorization ────────────────────────────────


async def categorize_transaction(db: AsyncSession, tx: Transaction) -> bool:
    """Apply rules to `tx`. Returns True if matched (and sets `tx.category_id`).

    Only applies a rule when the rule's target category kind matches the
    transaction type (FIX-14: kind guard).
    """
    from sqlalchemy.orm import selectinload  # local import to avoid circular

    stmt = (
        select(CategorizationRule)
        .options(selectinload(CategorizationRule.category))
        .where(CategorizationRule.enabled.is_(True))
        .order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
    )
    rules = (await db.execute(stmt)).scalars().all()

    for rule in rules:
        category = rule.category
        if category is None or category.kind != tx.type:
            logger.debug(
                "rule_skipped_kind_mismatch",
                rule_id=rule.id,
                category_kind=category.kind if category else None,
                tx_type=tx.type,
            )
            continue
        value = _field_value(tx, rule.field)
        if _match(rule, value):
            tx.category_id = rule.category_id
            rule.hit_count += 1
            return True
    return False


def _field_value(tx: Transaction, field: str) -> str:
    return {
        "description": tx.description or "",
        "counterparty": tx.counterparty or "",
        "raw_description": tx.raw_description or "",
    }.get(field, "")


def _match(rule: CategorizationRule, value: str) -> bool:
    if not value:
        return False
    if rule.pattern_type == "contains":
        return rule.pattern.lower() in value.lower()
    elif rule.pattern_type == "exact":
        return rule.pattern.lower() == value.lower()
    elif rule.pattern_type == "starts_with":
        return value.lower().startswith(rule.pattern.lower())
    elif rule.pattern_type == "regex":
        return _safe_regex_search(rule.pattern, value)
    return False


# ─── Reverse: learn from user assignment ───────────────────────────────


# Minimum keyword length to seed a learned rule. Below this we'd over-match
# (e.g. a 2-letter "AG" snippet would match thousands of merchants).
_MIN_LEARN_LEN = 4
# Words to drop when shrinking a long description down to a stable merchant token.
_NOISE_TOKENS = {
    # generic verbs / metadata
    "kauf", "zahlung", "purchase", "payment", "from", "to", "via", "at",
    "card", "fee", "transfer", "credit", "debit", "wechselkurs",
    # location-ish noise
    "berlin", "munich", "hamburg", "germany", "deutschland",
    # status words
    "pending", "completed", "successful",
}


def _extract_keyword(text: str) -> str | None:
    """Pick a stable substring from a transaction description to use as a learned rule.

    Strategy: take the first non-noise token >= _MIN_LEARN_LEN chars. Tokens are
    split on whitespace + common separators. We DON'T return the full description
    because real-world descriptions carry transaction IDs / dates that vary every
    time (e.g. 'Kauf 69F20393 UZR*Warehouse One Nürnberger Str. 23' should learn
    the merchant token 'Warehouse', not the whole string).
    """
    if not text:
        return None
    # Replace common separators with space, then split
    normalized = re.sub(r"[*/\-,.;:|]", " ", text)
    for tok in normalized.split():
        # Strip surrounding punctuation
        tok = tok.strip("\"'()[]{}")
        if len(tok) < _MIN_LEARN_LEN:
            continue
        if tok.lower() in _NOISE_TOKENS:
            continue
        # Skip pure-numeric tokens (transaction IDs, amounts)
        if tok.replace(".", "").replace(",", "").isdigit():
            continue
        # Skip tokens that are 80%+ digits (e.g. "69F20393")
        digit_ratio = sum(c.isdigit() for c in tok) / len(tok)
        if digit_ratio > 0.5:
            continue
        return tok
    return None


async def apply_to_similar_pending(
    db: AsyncSession,
    seed_tx: Transaction,
    category_id: int,
) -> int:
    """Cascade `category_id` to ALL other tx with the same description.

    Trigger: user just (re)classified `seed_tx`. Apply the same category to:
      - any pending tx with this description (clears the inbox in bulk)
      - any auto-categorised tx that ended up in the WRONG category, so that
        a single correction in the breakdown view fixes the entire batch
        of identical PDF rows

    Excludes:
      - the seed itself
      - rows already in the target category (no-op, avoids needless updates)
      - rows the user manually entered (`source='manual'`) — those carry a
        more authoritative classification choice we shouldn't overwrite
      - transfer-tagged rows (subaccount / cross-bank pairs) — re-categorising
        them would make no sense; they're not income/expense

    Returns the count of rows updated. Caller must recompute cash-flow snapshots.
    """
    # Defense-in-depth: verify the category kind matches the seed transaction type.
    # The API already validates this, but guard here too (FIX-14).
    cat_result = await db.execute(select(Category).where(Category.id == category_id))
    new_cat = cat_result.scalar_one_or_none()
    if new_cat is not None and new_cat.kind != seed_tx.type:
        logger.warning(
            "apply_to_similar_kind_mismatch",
            seed_tx_id=seed_tx.id,
            category_id=category_id,
            category_kind=new_cat.kind,
            tx_type=seed_tx.type,
        )
        return 0

    if not seed_tx.description:
        return 0
    norm_desc = seed_tx.description.strip()
    if not norm_desc:
        return 0
    # Sprint 4 FIX-24 (review V3 §V3-P2-1): the previous filter
    # `category_id != category_id` evaluates to NULL (not TRUE) for unset
    # categories, so pending rows with `category_id IS NULL` were silently
    # excluded — the "改 1 笔，同描述兄弟全跟着改" feature didn't actually
    # cover the most common case (un-categorised pending sibling).
    stmt = (
        select(Transaction)
        .where(
            Transaction.id != seed_tx.id,
            Transaction.deleted_at.is_(None),
            Transaction.description == norm_desc,
            or_(
                Transaction.category_id.is_(None),
                Transaction.category_id != category_id,
            ),
            Transaction.source != "manual",
            Transaction.type != "transfer",
            # Only cascade within the same kind. When the user crosses kinds
            # (e.g. expense → income on this row), don't drag other identical
            # rows along — they may have legitimately been the original kind
            # and rewriting their type would silently flip their sign.
            Transaction.type == seed_tx.type,
        )
    )
    rows = (await db.execute(stmt)).scalars().all()
    count = 0
    for r in rows:
        r.category_id = category_id
        # If it was still pending, confirming the cascade also clears it
        if r.is_pending:
            r.is_pending = False
        count += 1
    if count:
        await db.flush()
        logger.info("apply_to_similar", seed_id=seed_tx.id, count=count, desc=norm_desc[:60])
    return count


async def learn_from_user_assignment(
    db: AsyncSession,
    tx: Transaction,
    new_category_id: int,
) -> dict:
    """Create or strengthen a rule based on a user's manual category choice.

    Returns dict with:
        action: "created" | "bumped" | "skipped"
        rule_id: int | None
        keyword: str | None
        reason: str (when skipped)
    """
    # Prefer counterparty (often the cleanest merchant identifier), then description
    source_field, source_value = "description", tx.description or ""
    if tx.counterparty and len(tx.counterparty) >= _MIN_LEARN_LEN:
        source_field, source_value = "counterparty", tx.counterparty

    keyword = _extract_keyword(source_value)
    if not keyword:
        return {"action": "skipped", "rule_id": None, "keyword": None,
                "reason": "no_stable_keyword"}

    # Check for an existing rule with the same keyword targeting this category
    same = await db.execute(
        select(CategorizationRule).where(
            CategorizationRule.pattern.ilike(keyword),
            CategorizationRule.field == source_field,
            CategorizationRule.category_id == new_category_id,
        )
    )
    existing = same.scalar_one_or_none()
    if existing:
        existing.priority = (existing.priority or 0) + 1
        existing.enabled = True
        logger.info("rule_strengthened", rule_id=existing.id, keyword=keyword,
                    new_priority=existing.priority)
        return {"action": "bumped", "rule_id": existing.id, "keyword": keyword, "reason": ""}

    # Check for a same-keyword rule pointing to a DIFFERENT category — that's a conflict.
    conflict_q = await db.execute(
        select(CategorizationRule).where(
            CategorizationRule.pattern.ilike(keyword),
            CategorizationRule.field == source_field,
            CategorizationRule.category_id != new_category_id,
        )
    )
    conflict = conflict_q.scalar_one_or_none()
    if conflict:
        # User is overriding an existing rule. Disable the old one, create the new.
        conflict.enabled = False
        logger.info("rule_overridden", old_rule_id=conflict.id, keyword=keyword)

    rule = CategorizationRule(
        pattern=keyword,
        pattern_type="contains",
        field=source_field,
        category_id=new_category_id,
        priority=5,  # learned rules sit between user-edited (high) and seeds (priority 10)
        enabled=True,
        hit_count=1,
    )
    db.add(rule)
    await db.flush()
    logger.info("rule_learned", rule_id=rule.id, keyword=keyword, field=source_field,
                category_id=new_category_id)
    return {"action": "created", "rule_id": rule.id, "keyword": keyword, "reason": ""}
