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

import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CategorizationRule, Transaction

logger = structlog.get_logger(__name__)


# ─── Forward: rule-based categorization ────────────────────────────────


async def categorize_transaction(db: AsyncSession, tx: Transaction) -> bool:
    """Apply rules to `tx`. Returns True if matched (and sets `tx.category_id`)."""
    stmt = (
        select(CategorizationRule)
        .where(CategorizationRule.enabled.is_(True))
        .order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
    )
    rules = (await db.execute(stmt)).scalars().all()

    for rule in rules:
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
        try:
            return bool(re.search(rule.pattern, value, re.IGNORECASE))
        except re.error:
            return False
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
    if not seed_tx.description:
        return 0
    norm_desc = seed_tx.description.strip()
    if not norm_desc:
        return 0
    stmt = (
        select(Transaction)
        .where(
            Transaction.id != seed_tx.id,
            Transaction.deleted_at.is_(None),
            Transaction.description == norm_desc,
            Transaction.category_id != category_id,  # skip same-category no-ops
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
