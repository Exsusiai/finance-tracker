"""Categorization rules routes — CRUD + test + apply-all."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import require_auth
from app.core.errors import InvalidInputError, NotFoundError  # noqa: F401 (InvalidInputError re-raised by validator)
from app.db import get_db
from app.models import CategorizationRule, Transaction, Category
from app.models import touch_updated_at
from app.services.categorizer.engine import validate_regex_complexity, _safe_regex_search
from app.schemas import (
    ApiSuccess,
    RuleCreate,
    RuleOut,
    RuleTestIn,
    RuleTestOut,
    RuleUpdate,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


def _rule_to_out(r: CategorizationRule) -> RuleOut:
    return RuleOut(
        id=r.id,
        pattern=r.pattern,
        pattern_type=r.pattern_type,
        field=r.field,
        category_id=r.category_id,
        category_name=r.category.name if r.category else None,
        priority=r.priority,
        enabled=r.enabled,
        hit_count=r.hit_count,
        created_at=r.created_at,
    )


def _match_rule(rule: CategorizationRule, value: str) -> bool:
    """Test if a rule's pattern matches the given value."""
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


@router.get("", response_model=ApiSuccess[list[RuleOut]])
async def list_rules(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    category_id: int | None = Query(None),
    enabled_only: bool = Query(False),
):
    stmt = select(CategorizationRule).options(selectinload(CategorizationRule.category)).order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
    if category_id is not None:
        stmt = stmt.where(CategorizationRule.category_id == category_id)
    if enabled_only:
        stmt = stmt.where(CategorizationRule.enabled.is_(True))
    result = await db.execute(stmt)
    rules = result.scalars().all()
    return ApiSuccess(data=[_rule_to_out(r) for r in rules])


@router.post("", response_model=ApiSuccess[RuleOut], status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    if body.pattern_type == "regex":
        validate_regex_complexity(body.pattern)

    # Ensure the target category exists (FIX-14: write-time guard).
    cat_result = await db.execute(select(Category).where(Category.id == body.category_id))
    if cat_result.scalar_one_or_none() is None:
        raise NotFoundError("Category", body.category_id)

    rule = CategorizationRule(
        pattern=body.pattern,
        pattern_type=body.pattern_type,
        field=body.field,
        category_id=body.category_id,
        priority=body.priority,
        enabled=body.enabled,
    )
    db.add(rule)
    await db.flush()
    # Re-fetch with category loaded for async safety
    stmt = select(CategorizationRule).options(selectinload(CategorizationRule.category)).where(CategorizationRule.id == rule.id)
    result = await db.execute(stmt)
    rule = result.scalar_one()
    return ApiSuccess(data=_rule_to_out(rule))


@router.patch("/{rule_id}", response_model=ApiSuccess[RuleOut])
async def update_rule(
    rule_id: int,
    body: RuleUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(CategorizationRule)
        .options(selectinload(CategorizationRule.category))
        .where(CategorizationRule.id == rule_id)
    )
    result = await db.execute(stmt)
    rule = result.scalar_one_or_none()
    if not rule:
        raise NotFoundError("CategorizationRule", rule_id)

    updates = body.model_dump(exclude_unset=True)
    new_pattern_type = updates.get("pattern_type", rule.pattern_type)
    new_pattern = updates.get("pattern", rule.pattern)
    if new_pattern_type == "regex":
        validate_regex_complexity(new_pattern)

    # If category_id is being updated, verify the new category exists (FIX-14).
    if "category_id" in updates:
        new_cat_id = updates["category_id"]
        cat_result = await db.execute(select(Category).where(Category.id == new_cat_id))
        if cat_result.scalar_one_or_none() is None:
            raise NotFoundError("Category", new_cat_id)

    for key, value in updates.items():
        setattr(rule, key, value)

    touch_updated_at(rule)
    await db.flush()
    return ApiSuccess(data=_rule_to_out(rule))


@router.delete("/{rule_id}", response_model=ApiSuccess[dict])
async def delete_rule(
    rule_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(CategorizationRule).where(CategorizationRule.id == rule_id)
    result = await db.execute(stmt)
    rule = result.scalar_one_or_none()
    if not rule:
        raise NotFoundError("CategorizationRule", rule_id)

    await db.delete(rule)
    await db.flush()
    return ApiSuccess(data={"id": rule_id, "deleted": True})


@router.post("/test", response_model=ApiSuccess[RuleTestOut])
async def test_rule(
    body: RuleTestIn,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Test categorization rules against a description. Returns the first matching rule."""
    # Get all enabled rules ordered by priority
    stmt = (
        select(CategorizationRule)
        .options(selectinload(CategorizationRule.category))
        .where(CategorizationRule.enabled.is_(True))
        .order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
    )
    result = await db.execute(stmt)
    rules = result.scalars().all()

    for rule in rules:
        value = getattr(body, rule.field, None) or ""
        if _match_rule(rule, value):
            # Increment hit count
            rule.hit_count += 1
            await db.flush()

            return ApiSuccess(data=RuleTestOut(
                matched=True,
                rule_id=rule.id,
                category_id=rule.category_id,
                category_name=rule.category.name if rule.category else None,
            ))

    return ApiSuccess(data=RuleTestOut(matched=False))


@router.post("/apply-all", response_model=ApiSuccess[dict])
async def apply_rules_to_all(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Re-run all enabled rules against all uncategorized transactions."""
    # Get all enabled rules with their categories loaded (FIX-14: kind guard).
    rule_stmt = (
        select(CategorizationRule)
        .options(selectinload(CategorizationRule.category))
        .where(CategorizationRule.enabled.is_(True))
        .order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
    )
    rule_result = await db.execute(rule_stmt)
    rules = rule_result.scalars().all()

    # Get all uncategorized transactions
    tx_stmt = (
        select(Transaction)
        .where(Transaction.deleted_at.is_(None), Transaction.category_id.is_(None))
    )
    tx_result = await db.execute(tx_stmt)
    transactions = tx_result.scalars().all()

    updated = 0
    for tx in transactions:
        for rule in rules:
            # Skip if category kind does not match transaction type (FIX-14).
            if rule.category is None or rule.category.kind != tx.type:
                continue

            value = ""
            if rule.field == "description":
                value = tx.description or ""
            elif rule.field == "counterparty":
                value = tx.counterparty or ""
            elif rule.field == "raw_description":
                value = tx.raw_description or ""

            if _match_rule(rule, value):
                tx.category_id = rule.category_id
                rule.hit_count += 1
                updated += 1
                break  # First match wins

    await db.flush()
    return ApiSuccess(data={
        "matched": updated,
        "total_rules": len(rules),
        "total_uncategorized": len(transactions),
    })
