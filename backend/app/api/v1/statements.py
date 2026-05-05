"""PDF statement upload, parse, confirm, and management routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import require_auth
from app.core.config import get_settings
from app.core.errors import NotFoundError, ConflictError, ParserError
from app.db import get_db
from app.models import Transaction, PdfImport
from app.services.cashflow import parse_period, recompute_for_periods
from app.schemas import (
    ApiSuccess,
    PdfImportOut,
    TransactionOut,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]
settings = get_settings()


def _pdf_to_out(p: PdfImport, preview_txs: list[TransactionOut] | None = None) -> PdfImportOut:
    return PdfImportOut(
        id=p.id,
        filename=p.filename,
        file_hash=p.file_hash,
        file_size=p.file_size,
        detected_bank=p.detected_bank,
        parser_version=p.parser_version,
        account_id=p.account_id,
        statement_period=p.statement_period,
        transactions_count=p.transactions_count,
        status=p.status,
        error_message=p.error_message,
        preview=preview_txs or [],
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _tx_to_out(t: Transaction) -> TransactionOut:
    import json
    tags = []
    if t.tags_json:
        try:
            tags = json.loads(t.tags_json)
        except (json.JSONDecodeError, TypeError):
            tags = []
    return TransactionOut(
        id=t.id,
        account_id=t.account_id,
        account_name=t.account.name if t.account else None,
        counter_account_id=t.counter_account_id,
        category_id=t.category_id,
        category_name=t.category.name if t.category else None,
        occurred_at=t.occurred_at,
        posted_at=t.posted_at,
        amount=str(t.amount),
        currency=t.currency,
        fx_rate_to_base=str(t.fx_rate_to_base) if t.fx_rate_to_base else None,
        base_amount=str(t.base_amount) if t.base_amount else None,
        type=t.type,
        description=t.description,
        raw_description=t.raw_description,
        counterparty=t.counterparty,
        location=t.location,
        tags=tags,
        source=t.source,
        pdf_import_id=t.pdf_import_id,
        external_id=t.external_id,
        is_pending=t.is_pending,
        metadata_json=t.metadata_json,
        user_note=t.user_note,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.post("/upload", response_model=ApiSuccess[PdfImportOut])
async def upload_pdf(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    account_id: int | None = Query(None),
):
    """Upload a PDF bank statement for parsing."""
    # Read file content
    content = await file.read()
    if not content:
        raise ParserError("Empty file uploaded")

    file_hash = hashlib.sha256(content).hexdigest()

    # Check for duplicate
    existing = await db.execute(
        select(PdfImport).where(PdfImport.file_hash == file_hash)
    )
    if existing.scalar_one_or_none():
        raise ConflictError("PDF with identical content already imported")

    # Save to disk (sync IO offloaded to a worker thread)
    storage_dir = settings.pdf_storage_dir
    storage_path = storage_dir / f"{file_hash}.pdf"
    await asyncio.to_thread(storage_path.write_bytes, content)

    # Create import record
    pdf_import = PdfImport(
        filename=file.filename or "unknown.pdf",
        file_hash=file_hash,
        file_size=len(content),
        storage_path=str(storage_path),
        account_id=account_id,
        status="pending",
    )
    db.add(pdf_import)
    await db.flush()

    # Attempt parsing
    try:
        from app.services.pdf_parser.engine import parse_pdf_statement

        pdf_import.status = "parsing"
        await db.flush()

        # Look up the account's sub-account names so the parser can identify
        # internal moves (e.g. N26 main → "Investing" Space).
        subaccount_names: list[str] = []
        if account_id:
            from app.models import Account
            acct = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
            if acct and acct.metadata_json:
                try:
                    meta = json.loads(acct.metadata_json)
                    raw_names = meta.get("subaccount_names") if isinstance(meta, dict) else None
                    if isinstance(raw_names, list):
                        subaccount_names = [str(n) for n in raw_names if str(n).strip()]
                except (json.JSONDecodeError, TypeError):
                    pass

        result = await parse_pdf_statement(
            db, pdf_import, content, subaccount_names=subaccount_names,
        )
        pdf_import.detected_bank = result.get("detected_bank")
        pdf_import.parser_version = result.get("parser_version")
        pdf_import.statement_period = result.get("statement_period")
        pdf_import.raw_text = result.get("raw_text")
        pdf_import.error_message = result.get("error")
        pdf_import.status = "success" if not result.get("error") else "failed"

        if result.get("transactions"):
            from app.services.categorizer.engine import categorize_transaction
            for tx_data in result["transactions"]:
                tx = Transaction(
                    account_id=account_id or tx_data.get("account_id", 0),
                    occurred_at=tx_data.get("occurred_at", ""),
                    amount=Decimal(str(tx_data.get("amount", 0))),
                    currency=tx_data.get("currency", "CNY"),
                    type=tx_data.get("type", "expense"),
                    description=tx_data.get("description"),
                    raw_description=tx_data.get("raw_description"),
                    counterparty=tx_data.get("counterparty"),
                    source="pdf_import",
                    pdf_import_id=pdf_import.id,
                    external_id=tx_data.get("external_id"),
                    metadata_json=tx_data.get("metadata_json"),
                    # Default pending; auto-categorizer below promotes matched ones
                    is_pending=True,
                )
                db.add(tx)
                # Try rule-based auto-categorization.
                # If matched: keep is_pending=True so the user still confirms once
                # via the inbox (they can fast-accept the suggested category).
                # If not matched: stays pending without category — falls into inbox
                # for manual classification (which then triggers learning).
                await categorize_transaction(db, tx)

        await db.flush()
        pdf_import.transactions_count = len(result.get("transactions", []))

        # Run cross-account transfer matcher against the freshly imported batch
        if result.get("transactions"):
            from app.services.transfer_matcher import auto_pair_after_import
            new_tx_ids = [
                row.id for row in (await db.execute(
                    select(Transaction.id).where(Transaction.pdf_import_id == pdf_import.id)
                )).all()
            ]
            try:
                await auto_pair_after_import(db, new_tx_ids)
                await db.flush()
            except Exception as e:
                # Non-fatal: matcher errors shouldn't break the import
                import structlog
                structlog.get_logger(__name__).warning("transfer_matcher_failed", error=str(e))

        # Get preview (first 5) with eager-loaded relationships
        preview_stmt = (
            select(Transaction)
            .options(selectinload(Transaction.account), selectinload(Transaction.category))
            .where(Transaction.pdf_import_id == pdf_import.id)
            .order_by(Transaction.occurred_at)
            .limit(5)
        )
        preview_result = await db.execute(preview_stmt)
        preview_txs = [_tx_to_out(t) for t in preview_result.scalars().all()]

        return ApiSuccess(data=_pdf_to_out(pdf_import, preview_txs))

    except Exception as e:
        pdf_import.status = "failed"
        pdf_import.error_message = str(e)
        await db.flush()
        raise ParserError(f"Failed to parse PDF: {e}")


@router.get("", response_model=ApiSuccess[list[PdfImportOut]])
async def list_statements(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None),
):
    stmt = select(PdfImport).order_by(PdfImport.created_at.desc())
    if status:
        stmt = stmt.where(PdfImport.status == status)
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    imports = result.scalars().all()
    return ApiSuccess(data=[_pdf_to_out(p) for p in imports])


@router.get("/{import_id}", response_model=ApiSuccess[PdfImportOut])
async def get_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(PdfImport).where(PdfImport.id == import_id)
    result = await db.execute(stmt)
    pdf_import = result.scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    # Get transactions for preview
    tx_stmt = (
        select(Transaction)
        .where(Transaction.pdf_import_id == import_id)
        .order_by(Transaction.occurred_at)
        .limit(5)
    )
    tx_result = await db.execute(tx_stmt)
    preview_txs = [_tx_to_out(t) for t in tx_result.scalars().all()]

    return ApiSuccess(data=_pdf_to_out(pdf_import, preview_txs))


@router.post("/{import_id}/confirm", response_model=ApiSuccess[dict])
async def confirm_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Confirm all pending transactions from a PDF import."""
    stmt = select(PdfImport).where(PdfImport.id == import_id)
    result = await db.execute(stmt)
    pdf_import = result.scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    tx_stmt = (
        select(Transaction)
        .where(
            Transaction.pdf_import_id == import_id,
            Transaction.is_pending.is_(True),
        )
    )
    tx_result = await db.execute(tx_stmt)
    pending_txs = tx_result.scalars().all()

    count = 0
    affected_periods = []
    for tx in pending_txs:
        tx.is_pending = False
        count += 1
        affected_periods.append(parse_period(tx.occurred_at))

    await db.flush()
    # Confirmed transactions now contribute to cashflow — refresh affected snapshots
    await recompute_for_periods(db, affected_periods)
    return ApiSuccess(data={"import_id": import_id, "confirmed": count})


@router.post("/{import_id}/reparse", response_model=ApiSuccess[PdfImportOut])
async def reparse_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Re-parse a PDF import (useful after parser upgrades)."""
    stmt = select(PdfImport).where(PdfImport.id == import_id)
    result = await db.execute(stmt)
    pdf_import = result.scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    # Read stored PDF
    storage_path = pdf_import.storage_path
    if not os.path.exists(storage_path):
        raise ParserError(f"PDF file not found at {storage_path}")

    with open(storage_path, "rb") as f:
        content = f.read()

    # Delete old transactions from this import
    old_txs_stmt = select(Transaction).where(Transaction.pdf_import_id == import_id)
    old_txs_result = await db.execute(old_txs_stmt)
    for old_tx in old_txs_result.scalars().all():
        await db.delete(old_tx)
    await db.flush()

    # Re-parse
    try:
        from app.services.pdf_parser.engine import parse_pdf_statement

        pdf_import.status = "parsing"
        await db.flush()

        result = await parse_pdf_statement(db, pdf_import, content)
        pdf_import.detected_bank = result.get("detected_bank")
        pdf_import.parser_version = result.get("parser_version")
        pdf_import.statement_period = result.get("statement_period")
        pdf_import.raw_text = result.get("raw_text")
        pdf_import.error_message = result.get("error")
        pdf_import.status = "success" if not result.get("error") else "failed"

        for tx_data in result.get("transactions", []):
            tx = Transaction(
                account_id=pdf_import.account_id or tx_data.get("account_id", 0),
                occurred_at=tx_data.get("occurred_at", ""),
                amount=Decimal(str(tx_data.get("amount", 0))),
                currency=tx_data.get("currency", "CNY"),
                type=tx_data.get("type", "expense"),
                description=tx_data.get("description"),
                raw_description=tx_data.get("raw_description"),
                counterparty=tx_data.get("counterparty"),
                source="pdf_import",
                pdf_import_id=pdf_import.id,
                external_id=tx_data.get("external_id"),
                is_pending=True,
            )
            db.add(tx)

        await db.flush()
        pdf_import.transactions_count = len(result.get("transactions", []))

        preview_stmt = (
            select(Transaction)
            .where(Transaction.pdf_import_id == import_id)
            .order_by(Transaction.occurred_at)
            .limit(5)
        )
        preview_result = await db.execute(preview_stmt)
        preview_txs = [_tx_to_out(t) for t in preview_result.scalars().all()]

        return ApiSuccess(data=_pdf_to_out(pdf_import, preview_txs))

    except Exception as e:
        pdf_import.status = "failed"
        pdf_import.error_message = str(e)
        await db.flush()
        raise ParserError(f"Re-parse failed: {e}")


@router.delete("/{import_id}", response_model=ApiSuccess[dict])
async def delete_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Delete a PDF import and all its associated transactions."""
    stmt = select(PdfImport).where(PdfImport.id == import_id)
    result = await db.execute(stmt)
    pdf_import = result.scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    # Soft-delete associated transactions
    tx_stmt = select(Transaction).where(Transaction.pdf_import_id == import_id)
    tx_result = await db.execute(tx_stmt)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for tx in tx_result.scalars().all():
        tx.deleted_at = now

    await db.delete(pdf_import)
    await db.flush()
    return ApiSuccess(data={"id": import_id, "deleted": True})
