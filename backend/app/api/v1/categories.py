"""Category routes — CRUD + tree view."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.errors import NotFoundError, AppError
from app.db import get_db
from app.models import Category, CategorizationRule
from app.schemas import (
    ApiSuccess,
    CategoryCreate,
    CategoryOut,
    CategoryTree,
    CategoryUpdate,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


def _cat_to_out(c: Category) -> CategoryOut:
    return CategoryOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        parent_id=c.parent_id,
        icon=c.icon,
        color=c.color,
        sort_order=c.sort_order,
        is_system=c.is_system,
        created_at=c.created_at,
    )


@router.get("", response_model=ApiSuccess[list[CategoryOut]])
async def list_categories(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    kind: str | None = Query(None, pattern=r"^(expense|income|transfer)$"),
):
    stmt = select(Category)
    if kind:
        stmt = stmt.where(Category.kind == kind)
    stmt = stmt.order_by(Category.kind, Category.sort_order, Category.id)
    result = await db.execute(stmt)
    categories = result.scalars().all()
    return ApiSuccess(data=[_cat_to_out(c) for c in categories])


@router.get("/tree", response_model=ApiSuccess[list[CategoryTree]])
async def category_tree(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Category).order_by(Category.kind, Category.sort_order, Category.id)
    result = await db.execute(stmt)
    all_cats = result.scalars().all()

    # Build tree structure
    cat_map: dict[int, CategoryTree] = {}
    roots: list[CategoryTree] = []

    for c in all_cats:
        node = CategoryTree(
            id=c.id,
            name=c.name,
            kind=c.kind,
            icon=c.icon,
            color=c.color,
            sort_order=c.sort_order,
            is_system=c.is_system,
        )
        cat_map[c.id] = node
        if c.parent_id is None or c.parent_id not in cat_map:
            roots.append(node)
        else:
            cat_map[c.parent_id].children.append(node)

    return ApiSuccess(data=roots)


@router.post("", response_model=ApiSuccess[CategoryOut], status_code=201)
async def create_category(
    body: CategoryCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    cat = Category(
        name=body.name,
        kind=body.kind,
        parent_id=body.parent_id,
        icon=body.icon,
        color=body.color,
        sort_order=body.sort_order,
    )
    db.add(cat)
    await db.flush()
    return ApiSuccess(data=_cat_to_out(cat))


@router.patch("/{category_id}", response_model=ApiSuccess[CategoryOut])
async def update_category(
    category_id: int,
    body: CategoryUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Category).where(Category.id == category_id)
    result = await db.execute(stmt)
    cat = result.scalar_one_or_none()
    if not cat:
        raise NotFoundError("Category", category_id)

    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(cat, key, value)

    await db.flush()
    return ApiSuccess(data=_cat_to_out(cat))


@router.delete("/{category_id}", response_model=ApiSuccess[dict])
async def delete_category(
    category_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Category).where(Category.id == category_id)
    result = await db.execute(stmt)
    cat = result.scalar_one_or_none()
    if not cat:
        raise NotFoundError("Category", category_id)

    if cat.is_system:
        raise AppError(
            code="INVALID_INPUT",
            message="System categories cannot be deleted",
            status_code=400,
        )

    await db.delete(cat)
    await db.flush()
    return ApiSuccess(data={"id": category_id, "deleted": True})
