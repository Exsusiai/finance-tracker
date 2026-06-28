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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import require_auth
from app.core.config import get_settings
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, ParserError
from app.db import get_db
from app.models import Account, Transaction, PdfImport
from app.services.cashflow import parse_period, recompute_for_periods
from app.schemas import (
    ApiSuccess,
    ParsedPreviewTx,
    PdfImportOut,
    TransactionOut,
)

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]
settings = get_settings()

MAX_PDF_SIZE_MB = 10


def _pdf_to_out(
    p: PdfImport,
    preview_txs: list[TransactionOut] | None = None,
    parsed_preview: list[ParsedPreviewTx] | None = None,
    reconciliation: dict | None = None,
) -> PdfImportOut:
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
        parsed_preview=parsed_preview or [],
        reconciliation=reconciliation,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _mark_failed(db: AsyncSession, import_id: int, msg: str) -> None:
    """Record a parse/commit failure on the import WITHOUT crashing the
    handler.

    A mid-pipeline exception (e.g. an IntegrityError on flush) leaves the
    request session needing a rollback. We roll it back first to clear the
    pending state, then persist the failed status on a **separate** session
    that we commit ourselves.

    V8-P2-2: the failed status used to be flushed on the request session, but
    the caller re-raises afterwards and ``get_db`` rolls the request session
    back on the exception path — so the 'failed' + error_message were lost and
    the import stayed stuck in awaiting_review / parsing. A dedicated committed
    session makes the failure durable regardless of the outer rollback.
    """
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.db import async_session_factory

        async with async_session_factory() as fail_session:
            p = (await fail_session.execute(
                select(PdfImport).where(PdfImport.id == import_id)
            )).scalar_one_or_none()
            if p is not None:
                p.status = "failed"
                p.error_message = (msg or "")[:500]
                await fail_session.commit()
    except Exception:  # noqa: BLE001
        pass


def _parsed_preview_from_result(result: dict) -> list[ParsedPreviewTx]:
    """Build the pre-commit preview (ALL parsed rows) from parser output."""
    return [
        ParsedPreviewTx(
            occurred_at=t.get("occurred_at"),
            amount=str(t.get("amount", "0")),
            currency=t.get("currency"),
            type=t.get("type"),
            description=t.get("description"),
        )
        for t in result.get("transactions", [])
    ]


async def _load_subaccount_names(db: AsyncSession, account_id: int | None) -> list[str]:
    if not account_id:
        return []
    from app.models import Account

    acct = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
    if not (acct and acct.metadata_json):
        return []
    try:
        meta = json.loads(acct.metadata_json)
        raw = meta.get("subaccount_names") if isinstance(meta, dict) else None
        return [str(n) for n in raw if str(n).strip()] if isinstance(raw, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _resolve_candidate_account(
    db: AsyncSession, detected_bank: str | None, has_transactions: bool
) -> int | None:
    """Best-effort pick of the account a statement belongs to.

    1) exactly one active account → that one
    2) detected_bank uniquely matches an account's institution / name
    Returns None when ambiguous (the user then picks in the preview UI).
    """
    if not has_transactions:
        return None
    from app.models import Account

    active = (await db.execute(
        select(Account).where(
            Account.deleted_at.is_(None), Account.is_active == True  # noqa: E712
        )
    )).scalars().all()
    if len(active) == 1:
        return active[0].id
    if active and detected_bank:
        bank = detected_bank.lower()
        matches = [
            a for a in active
            if (a.institution or "").lower().replace(" ", "").startswith(
                bank.replace("_", "").replace("-", "")
            )
            or bank in (a.institution or "").lower()
            or bank in (a.name or "").lower()
        ]
        if len(matches) == 1:
            return matches[0].id
    return None


async def _insert_and_ingest(
    db: AsyncSession, pdf_import: PdfImport, result: dict, account_id: int
) -> None:
    """Create Transaction rows from parser output + run the ingestion pipeline.

    Shared by commit / assign-account / reparse so the insert path stays
    identical (amount normalize → categorize → transfer-match → recompute)."""
    new_txs: list[Transaction] = []
    for tx_data in result.get("transactions", []):
        tx = Transaction(
            account_id=account_id,
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
            is_pending=True,
        )
        db.add(tx)
        new_txs.append(tx)
    if new_txs:
        from app.services.ingestion import ingest_transactions

        await ingest_transactions(db, new_txs, auto_pair=True)


async def _reconcile_commit(
    db: AsyncSession, pdf_import: PdfImport, account, result: dict
) -> dict | None:
    """Compute statement→ledger reconciliation after a commit.

    `as_of` = the statement's last transaction date (the closing balance is
    "after" that row), so Σ(tx ≤ as_of) covers exactly this statement + prior.
    Returns None when the parser found no closing balance.
    """
    closing_raw = result.get("closing_balance")
    if closing_raw is None:
        return None
    as_of = (await db.execute(
        text("""
            SELECT MAX(occurred_at) FROM transactions
            WHERE pdf_import_id = :pid AND deleted_at IS NULL
        """),
        {"pid": pdf_import.id},
    )).scalar()
    if not as_of:
        return None
    from app.services.valuation.anchor import compute_reconciliation
    return await compute_reconciliation(db, account, Decimal(str(closing_raw)), as_of)


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
    bank_format: str | None = Query(
        None,
        description="Override bank-format auto-detection. One of "
        "n26 / revolut / tfbank / advanzia / amex_de / other(generic). "
        "Omit or 'auto' to let the parser detect from text features.",
    ),
):
    """Upload a PDF bank statement for parsing."""
    # Read file content
    content = await file.read()
    if not content:
        raise ParserError("Empty file uploaded")

    # Guard: reject oversized files before touching disk or the parser
    _max_bytes = MAX_PDF_SIZE_MB * 1024 * 1024
    if len(content) > _max_bytes:
        raise ParserError(
            f"Uploaded file exceeds the {MAX_PDF_SIZE_MB} MiB limit",
            details={"max_mb": MAX_PDF_SIZE_MB, "got_bytes": len(content)},
        )

    # Guard: require PDF magic bytes — reject non-PDF blobs early
    if not content.startswith(b"%PDF-"):
        raise ParserError("Uploaded file is not a valid PDF (missing %PDF- magic bytes)")

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

    # Parse + STAGE — never insert transactions here. The user reviews the
    # preview then commits (POST /statements/{id}/commit) or cancels
    # (DELETE /statements/{id}). Nothing touches the ledger until commit.
    try:
        from app.services.pdf_parser.engine import parse_pdf_statement

        pdf_import.status = "parsing"
        await db.flush()

        subaccount_names = await _load_subaccount_names(db, account_id)
        result = await parse_pdf_statement(
            db, pdf_import, content, subaccount_names=subaccount_names,
            force_bank=bank_format,
        )
        pdf_import.detected_bank = result.get("detected_bank")
        pdf_import.parser_version = result.get("parser_version")
        pdf_import.statement_period = result.get("statement_period")
        pdf_import.raw_text = result.get("raw_text")
        pdf_import.transactions_count = len(result.get("transactions", []))

        if result.get("error"):
            pdf_import.status = "failed"
            pdf_import.error_message = result.get("error")
            await db.flush()
            return ApiSuccess(data=_pdf_to_out(pdf_import))

        # Resolve a candidate account (caller-supplied wins; else best-effort).
        if account_id is None:
            account_id = await _resolve_candidate_account(
                db, pdf_import.detected_bank, bool(result.get("transactions"))
            )
        pdf_import.account_id = account_id  # may be None → user picks in UI

        # Land in awaiting_review with the FULL parsed preview. No inserts.
        pdf_import.status = "awaiting_review"
        pdf_import.error_message = None
        await db.flush()
        return ApiSuccess(
            data=_pdf_to_out(
                pdf_import, parsed_preview=_parsed_preview_from_result(result)
            )
        )

    except (InvalidInputError, ConflictError, NotFoundError, ParserError):
        raise
    except Exception as e:
        await _mark_failed(db, pdf_import.id, str(e))
        raise ParserError(f"Failed to parse PDF: {e}")


@router.post("/upload-csv", response_model=ApiSuccess[dict])
async def upload_csv(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    account_id: int = Query(..., description="Target account for the CSV rows"),
):
    """Import a CSV account export (currently: PayPal activity export).

    Unlike the PDF flow this imports directly (no preview/staging) and is
    safe to call with OVERLAPPING date ranges — rows are deduped by their
    ``external_id`` (PayPal Transaction ID), so re-uploading months that were
    already imported just skips the duplicates. Any date range / multiple
    months in one file is fine; the parser reads whatever rows are present.
    """
    from app.services.csv_import import import_csv_rows
    from app.services.csv_parser import detect_and_parse_csv

    content = await file.read()
    if not content:
        raise ParserError("Empty file uploaded")
    _max_bytes = MAX_PDF_SIZE_MB * 1024 * 1024
    if len(content) > _max_bytes:
        raise ParserError(
            f"Uploaded file exceeds the {MAX_PDF_SIZE_MB} MiB limit",
            details={"max_mb": MAX_PDF_SIZE_MB, "got_bytes": len(content)},
        )

    # Validate the target account exists.
    acct = (await db.execute(
        select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
    )).scalar_one_or_none()
    if acct is None:
        raise NotFoundError("Account", account_id)

    parse_result = detect_and_parse_csv(content)
    if parse_result.get("error"):
        raise ParserError(parse_result["error"])

    summary = await import_csv_rows(db, account_id, parse_result)
    return ApiSuccess(data={
        "detected_source": summary.detected_source,
        "period": summary.period,
        "parsed": summary.parsed,
        "imported": summary.imported,
        "skipped_duplicate": summary.skipped_duplicate,
        "skipped_no_external_id": summary.skipped_no_external_id,
    })


@router.get("", response_model=ApiSuccess[list[PdfImportOut]])
async def list_statements(
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
):
    # `meta.total` lets the UI show "showing N of M" + a load-more affordance.
    count_stmt = select(func.count(PdfImport.id))
    base = select(PdfImport).order_by(PdfImport.created_at.desc())
    if status:
        count_stmt = count_stmt.where(PdfImport.status == status)
        base = base.where(PdfImport.status == status)
    total = (await db.execute(count_stmt)).scalar() or 0
    result = await db.execute(base.limit(limit).offset(offset))
    imports = result.scalars().all()
    return ApiSuccess(
        data=[_pdf_to_out(p) for p in imports],
        meta={"total": int(total), "limit": limit, "offset": offset},
    )


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

    # Pre-commit (awaiting_review/awaiting_account): re-parse the stored PDF to
    # rebuild the full preview — nothing is in the ledger yet.
    if pdf_import.status in ("awaiting_review", "awaiting_account"):
        if os.path.exists(pdf_import.storage_path):
            from app.services.pdf_parser.engine import parse_pdf_statement

            content = await asyncio.to_thread(_read_file, pdf_import.storage_path)
            subaccount_names = await _load_subaccount_names(db, pdf_import.account_id)
            result = await parse_pdf_statement(
                db, pdf_import, content, subaccount_names=subaccount_names,
            )
            return ApiSuccess(
                data=_pdf_to_out(
                    pdf_import, parsed_preview=_parsed_preview_from_result(result)
                )
            )
        return ApiSuccess(data=_pdf_to_out(pdf_import))

    # Committed: preview the real Transaction rows.
    tx_stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.pdf_import_id == import_id)
        .order_by(Transaction.occurred_at)
        .limit(5)
    )
    tx_result = await db.execute(tx_stmt)
    preview_txs = [_tx_to_out(t) for t in tx_result.scalars().all()]

    return ApiSuccess(data=_pdf_to_out(pdf_import, preview_txs))


@router.post("/{import_id}/commit", response_model=ApiSuccess[PdfImportOut])
async def commit_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    account_id: int | None = Query(
        None,
        description="Target account. Optional — falls back to the candidate "
        "resolved at upload time. Required if no candidate was found.",
    ),
):
    """Commit a staged (awaiting_review) import: insert its transactions.

    This is the only path that writes a PDF's transactions into the ledger.
    Re-parses the stored PDF (authoritative) and runs the full ingestion
    pipeline. Cancelling instead = DELETE /statements/{id}.
    """
    pdf_import = (await db.execute(
        select(PdfImport).where(PdfImport.id == import_id)
    )).scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    if pdf_import.status not in ("awaiting_review", "awaiting_account"):
        raise InvalidInputError(
            f"This statement is in status '{pdf_import.status}'; commit only "
            "applies to a staged (awaiting_review) import.",
            details={"current_status": pdf_import.status},
        )

    target_account_id = account_id if account_id is not None else pdf_import.account_id
    if target_account_id is None:
        raise InvalidInputError(
            "请选择要导入到的账户。",
            details={"reason": "no_account"},
        )

    from app.models import Account

    target = (await db.execute(
        select(Account).where(
            Account.id == target_account_id, Account.deleted_at.is_(None)
        )
    )).scalar_one_or_none()
    if not target:
        raise NotFoundError("Account", target_account_id)

    if not os.path.exists(pdf_import.storage_path):
        raise ParserError(f"PDF file not found at {pdf_import.storage_path}")
    content = await asyncio.to_thread(_read_file, pdf_import.storage_path)
    subaccount_names = await _load_subaccount_names(db, target_account_id)

    from app.services.pdf_parser.engine import parse_pdf_statement

    pdf_import.status = "parsing"
    pdf_import.account_id = target_account_id
    await db.flush()
    try:
        result = await parse_pdf_statement(
            db, pdf_import, content, subaccount_names=subaccount_names,
        )
        pdf_import.detected_bank = result.get("detected_bank")
        pdf_import.parser_version = result.get("parser_version")
        pdf_import.statement_period = result.get("statement_period")
        pdf_import.raw_text = result.get("raw_text")
        pdf_import.error_message = result.get("error")
        pdf_import.status = "success" if not result.get("error") else "failed"
        pdf_import.transactions_count = len(result.get("transactions", []))

        await _insert_and_ingest(db, pdf_import, result, target_account_id)

        # Balance reconciliation: compare the statement's printed closing
        # balance against the computed ledger balance at the statement's last
        # transaction date. Surfaces drift (mis-recorded rows) and lets the
        # user one-click anchor. None when no closing balance was parsable.
        reconciliation = await _reconcile_commit(db, pdf_import, target, result)

        preview_stmt = (
            select(Transaction)
            .options(selectinload(Transaction.account), selectinload(Transaction.category))
            .where(Transaction.pdf_import_id == pdf_import.id)
            .order_by(Transaction.occurred_at)
            .limit(5)
        )
        preview_result = await db.execute(preview_stmt)
        preview_txs = [_tx_to_out(t) for t in preview_result.scalars().all()]
        return ApiSuccess(data=_pdf_to_out(pdf_import, preview_txs, reconciliation=reconciliation))
    except (InvalidInputError, ConflictError, NotFoundError, ParserError):
        raise
    except Exception as e:
        await _mark_failed(db, pdf_import.id, str(e))
        raise ParserError(f"Failed to commit import: {e}")


@router.post("/{import_id}/assign-account", response_model=ApiSuccess[PdfImportOut])
async def assign_account_to_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
    account_id: int = Query(..., description="Target account_id for the statement's transactions"),
):
    """Finish a PDF import that was left in `awaiting_account` status.

    The /upload endpoint stops short of writing transactions when it can't
    confidently auto-pick an account (e.g. user has multiple accounts and
    none has a name matching the parser's detected_bank). This endpoint
    re-parses the stored PDF, binds it to the user-chosen account, and runs
    the full ingestion pipeline.
    """
    from app.models import Account

    pdf_import = (await db.execute(
        select(PdfImport).where(PdfImport.id == import_id)
    )).scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    if pdf_import.status != "awaiting_account":
        raise InvalidInputError(
            f"This statement is in status '{pdf_import.status}'; "
            "assign-account only applies to 'awaiting_account' rows.",
            details={"current_status": pdf_import.status},
        )

    target = (await db.execute(
        select(Account).where(
            Account.id == account_id, Account.deleted_at.is_(None)
        )
    )).scalar_one_or_none()
    if not target:
        raise NotFoundError("Account", account_id)

    # Reload the stored PDF and re-parse with the resolved account.
    if not os.path.exists(pdf_import.storage_path):
        raise ParserError(f"PDF file not found at {pdf_import.storage_path}")
    with open(pdf_import.storage_path, "rb") as f:
        content = f.read()

    # Pull sub-account hints from the chosen account (same as upload).
    subaccount_names: list[str] = []
    if target.metadata_json:
        try:
            meta = json.loads(target.metadata_json)
            raw_names = meta.get("subaccount_names") if isinstance(meta, dict) else None
            if isinstance(raw_names, list):
                subaccount_names = [str(n) for n in raw_names if str(n).strip()]
        except (json.JSONDecodeError, TypeError):
            pass

    from app.services.pdf_parser.engine import parse_pdf_statement

    pdf_import.status = "parsing"
    pdf_import.account_id = account_id
    await db.flush()

    try:
        result = await parse_pdf_statement(
            db, pdf_import, content, subaccount_names=subaccount_names,
        )
        pdf_import.detected_bank = result.get("detected_bank")
        pdf_import.parser_version = result.get("parser_version")
        pdf_import.statement_period = result.get("statement_period")
        pdf_import.raw_text = result.get("raw_text")
        pdf_import.error_message = None  # clear the warning
        pdf_import.status = "success" if not result.get("error") else "failed"

        if result.get("transactions"):
            new_txs: list[Transaction] = []
            for tx_data in result["transactions"]:
                tx = Transaction(
                    account_id=account_id,
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
                    is_pending=True,
                )
                db.add(tx)
                new_txs.append(tx)

            from app.services.ingestion import ingest_transactions

            await ingest_transactions(db, new_txs, auto_pair=True)

        pdf_import.transactions_count = len(result.get("transactions", []))

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

    except (InvalidInputError, ConflictError, NotFoundError, ParserError):
        raise
    except Exception as e:
        await _mark_failed(db, pdf_import.id, str(e))
        raise ParserError(f"Failed to assign account and import: {e}")


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

    # 2026-05-06 fix: previously this endpoint翻了 ALL is_pending rows
    # 不论是否分类，结果未分类 tx 跑出了 inbox（用户报告："明明很多未
    # 分类的没有放在待确认里让我确认"）。新语义：
    #   - 已分类的 pending → 翻 false（自动通过）
    #   - 未分类的 pending → 保留 is_pending=True 留在 inbox 等用户操作
    # 这与 Sprint 0/1 的 inbox 工作流（FIX-7 / "高置信度自动通过 inbox"）
    # 一致：ingestion 命中规则的已经被自动通过，未命中的应该进 inbox。
    tx_stmt = (
        select(Transaction)
        .where(
            Transaction.pdf_import_id == import_id,
            Transaction.is_pending.is_(True),
            Transaction.category_id.is_not(None),  # 仅已分类
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

    # Count uncategorized rows that stayed in inbox so caller can show
    # "X confirmed, Y still in inbox awaiting category".
    inbox_remaining = (await db.execute(
        select(func.count(Transaction.id)).where(
            Transaction.pdf_import_id == import_id,
            Transaction.is_pending.is_(True),
            Transaction.category_id.is_(None),
            Transaction.deleted_at.is_(None),
        )
    )).scalar() or 0

    await db.flush()
    # Confirmed transactions now contribute to cashflow — refresh affected snapshots
    await recompute_for_periods(db, affected_periods)
    return ApiSuccess(data={
        "import_id": import_id,
        "confirmed": count,
        "inbox_remaining": int(inbox_remaining),
    })


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

    # V4-P2-1: an awaiting_account / orphan import has no resolved account
    # — the old `account_id=pdf_import.account_id or tx_data.get("account_id", 0)`
    # fallback would write account_id=0 and trigger an FK error. Refuse
    # explicitly with an actionable hint.
    if not pdf_import.account_id:
        raise InvalidInputError(
            "Cannot reparse: this import has no associated account. "
            "Use POST /statements/{id}/assign-account first.",
            details={"import_id": import_id, "status": pdf_import.status},
        )

    # Read stored PDF
    storage_path = pdf_import.storage_path
    if not os.path.exists(storage_path):
        raise ParserError(f"PDF file not found at {storage_path}")

    with open(storage_path, "rb") as f:
        content = f.read()

    # Sprint 1 FIX-4: collect periods of OLD rows before delete, so the
    # cashflow snapshots covering them get rewritten too (otherwise reparsing
    # a month leaves stale totals from the deleted-but-still-summed rows).
    old_txs_stmt = select(Transaction).where(Transaction.pdf_import_id == import_id)
    old_txs_result = await db.execute(old_txs_stmt)
    old_periods: set[tuple[int, int]] = set()
    for old_tx in old_txs_result.scalars().all():
        period = parse_period(old_tx.occurred_at)
        if period:
            old_periods.add(period)
        await db.delete(old_tx)
    await db.flush()

    # Re-parse
    try:
        from app.services.pdf_parser.engine import parse_pdf_statement

        pdf_import.status = "parsing"
        await db.flush()

        # Look up sub-account names so the parser can flag in-bank moves the
        # same way upload does.
        subaccount_names = await _load_subaccount_names(db, pdf_import.account_id)

        result = await parse_pdf_statement(
            db, pdf_import, content, subaccount_names=subaccount_names,
        )
        pdf_import.detected_bank = result.get("detected_bank")
        pdf_import.parser_version = result.get("parser_version")
        pdf_import.statement_period = result.get("statement_period")
        pdf_import.raw_text = result.get("raw_text")
        pdf_import.error_message = result.get("error")
        pdf_import.status = "success" if not result.get("error") else "failed"

        new_txs: list[Transaction] = []
        for tx_data in result.get("transactions", []):
            tx = Transaction(
                # V4-P2-1: account_id is guaranteed non-None at the top
                # of this route (we 422 if missing). Drop the `or 0`
                # fallback that would otherwise leak FK errors.
                account_id=pdf_import.account_id,
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
                # Sprint 1 FIX-4: preserve parser-emitted metadata (subaccount /
                # cross_bank_hint) — previously dropped on reparse.
                metadata_json=tx_data.get("metadata_json"),
                is_pending=True,
            )
            db.add(tx)
            new_txs.append(tx)

        # Sprint 1 FIX-4: route through unified ingestion (categorize +
        # transfer match + recompute). Previously reparse skipped all of this.
        from app.services.ingestion import ingest_transactions, recompute_after_delete

        ingest_result = await ingest_transactions(db, new_txs, auto_pair=True)
        # Also recompute periods that lost rows during the delete-old step
        # but didn't get re-covered by the new batch.
        stale_periods = old_periods - ingest_result.affected_periods
        if stale_periods:
            await recompute_after_delete(db, stale_periods)
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
        await _mark_failed(db, pdf_import.id, str(e))
        raise ParserError(f"Re-parse failed: {e}")


@router.delete("/{import_id}", response_model=ApiSuccess[dict])
async def delete_statement(
    import_id: int,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    """Delete/cancel a PDF import and all its associated transactions.

    Doubles as the cancel for a staged (awaiting_review) import: it removes
    the import record AND the stored PDF file, so a cancelled upload leaves no
    trace and can be re-uploaded fresh.
    """
    stmt = select(PdfImport).where(PdfImport.id == import_id)
    result = await db.execute(stmt)
    pdf_import = result.scalar_one_or_none()
    if not pdf_import:
        raise NotFoundError("PdfImport", import_id)

    # Soft-delete associated transactions and collect their months so the
    # cashflow snapshots get refreshed (Sprint 1 FIX-4 — review V1 §P1-3).
    # (Staged imports have none yet — this is a no-op for them.)
    tx_stmt = select(Transaction).where(Transaction.pdf_import_id == import_id)
    tx_result = await db.execute(tx_stmt)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    affected_periods: set[tuple[int, int]] = set()
    for tx in tx_result.scalars().all():
        tx.deleted_at = now
        period = parse_period(tx.occurred_at)
        if period:
            affected_periods.add(period)

    storage_path = pdf_import.storage_path
    await db.delete(pdf_import)
    await db.flush()
    if affected_periods:
        from app.services.ingestion import recompute_after_delete
        await recompute_after_delete(db, affected_periods)
    # Remove the stored PDF so a cancelled import leaves no trace (and the
    # SHA-256 dedup guard won't block re-uploading the same file later).
    if storage_path and os.path.exists(storage_path):
        try:
            await asyncio.to_thread(os.remove, storage_path)
        except OSError:
            pass  # best-effort; orphan file is harmless
    return ApiSuccess(data={"id": import_id, "deleted": True})
