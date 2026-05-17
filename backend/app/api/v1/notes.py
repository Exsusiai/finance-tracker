"""Categorization notes (knowledge base) routes — CRUD.

Notes are surfaced to the LLM as few-shot context. Most notes are auto-
created from inbox-confirm with a user_note attached, but they can also
be created/edited directly here from the Settings → 知识库 UI.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.core.errors import InvalidInputError, NotFoundError
from app.db import get_db
from app.models import CategorizationNote, Category, touch_updated_at
from app.schemas import ApiSuccess, NoteCreate, NoteOut, NoteUpdate

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


def _to_out(n: CategorizationNote, *, category_name: str | None = None) -> NoteOut:
    return NoteOut(
        id=n.id,
        category_id=n.category_id,
        category_name=category_name,
        trigger_text=n.trigger_text,
        note_text=n.note_text,
        source_transaction_id=n.source_transaction_id,
        usage_count=n.usage_count,
        enabled=n.enabled,
        created_at=n.created_at,
        updated_at=n.updated_at,
    )


@router.get("", response_model=ApiSuccess[list[NoteOut]])
async def list_notes(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    category_id: int | None = Query(None),
    enabled: bool | None = Query(None),
):
    stmt = select(CategorizationNote)
    if category_id is not None:
        stmt = stmt.where(CategorizationNote.category_id == category_id)
    if enabled is not None:
        stmt = stmt.where(CategorizationNote.enabled.is_(enabled))
    stmt = stmt.order_by(CategorizationNote.id.desc())
    rows = (await db.execute(stmt)).scalars().all()
    cat_ids = {r.category_id for r in rows}
    cats = (
        await db.execute(select(Category).where(Category.id.in_(cat_ids)))
    ).scalars().all()
    cat_name_by_id = {c.id: c.name for c in cats}
    return ApiSuccess(
        data=[_to_out(r, category_name=cat_name_by_id.get(r.category_id)) for r in rows]
    )


@router.post("", response_model=ApiSuccess[NoteOut])
async def create_note(
    body: NoteCreate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    cat = (
        await db.execute(select(Category).where(Category.id == body.category_id))
    ).scalar_one_or_none()
    if cat is None:
        raise InvalidInputError(f"Category {body.category_id} not found")
    note = CategorizationNote(
        category_id=body.category_id,
        trigger_text=body.trigger_text.strip(),
        note_text=body.note_text.strip(),
        usage_count=0,
        enabled=body.enabled,
    )
    db.add(note)
    await db.flush()
    return ApiSuccess(data=_to_out(note, category_name=cat.name))


@router.patch("/{note_id}", response_model=ApiSuccess[NoteOut])
async def update_note(
    note_id: int,
    body: NoteUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    note = (
        await db.execute(select(CategorizationNote).where(CategorizationNote.id == note_id))
    ).scalar_one_or_none()
    if note is None:
        raise NotFoundError("CategorizationNote", note_id)
    update_data = body.model_dump(exclude_unset=True)
    if "category_id" in update_data:
        cat = (
            await db.execute(select(Category).where(Category.id == update_data["category_id"]))
        ).scalar_one_or_none()
        if cat is None:
            raise InvalidInputError(f"Category {update_data['category_id']} not found")
    for key, value in update_data.items():
        if isinstance(value, str):
            value = value.strip()
        setattr(note, key, value)
    touch_updated_at(note)
    await db.flush()
    cat_name = None
    if note.category_id:
        cat = (
            await db.execute(select(Category).where(Category.id == note.category_id))
        ).scalar_one_or_none()
        cat_name = cat.name if cat else None
    return ApiSuccess(data=_to_out(note, category_name=cat_name))


@router.delete("/{note_id}", response_model=ApiSuccess[dict])
async def delete_note(
    note_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    note = (
        await db.execute(select(CategorizationNote).where(CategorizationNote.id == note_id))
    ).scalar_one_or_none()
    if note is None:
        raise NotFoundError("CategorizationNote", note_id)
    # Hard delete — notes have no foreign-key dependents that need preservation.
    await db.delete(note)
    await db.flush()
    return ApiSuccess(data={"deleted": True, "id": note_id})
