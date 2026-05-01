"""Categorization engine — rule-based transaction categorization."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CategorizationRule, Transaction


async def categorize_transaction(db: AsyncSession, tx: Transaction) -> bool:
    """Run all enabled categorization rules against a transaction.
    Returns True if a match was found and category was set."""
    stmt = (
        select(CategorizationRule)
        .where(CategorizationRule.enabled.is_(True))
        .order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
    )
    result = await db.execute(stmt)
    rules = result.scalars().all()

    for rule in rules:
        value = ""
        if rule.field == "description":
            value = tx.description or ""
        elif rule.field == "counterparty":
            value = tx.counterparty or ""
        elif rule.field == "raw_description":
            value = tx.raw_description or ""

        if _match(rule, value):
            tx.category_id = rule.category_id
            rule.hit_count += 1
            return True

    return False


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
